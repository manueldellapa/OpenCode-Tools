"""Read-only Git target preflight (System Design SS11.1; ADR-003/004/009/010).

This module proves, before any agent runs, that a target repository is safe
to operate on: contained in the workspace after symlink resolution, the Git
top-level of a non-bare repository, on an attached branch with a resolvable
`HEAD`, and free of staged, unstaged, or untracked changes. Every probe is
read-only, addresses the target explicitly with `git -C <target>` -- never
an implicit `cwd`/workspace -- and shares one bounded `utility_timeout_seconds`
deadline. An ambiguous, timed-out, or non-zero probe result fails closed
rather than being treated as clean (AC-004-AC-006, AC-036).

Fingerprinting (`git-state-v1`), per-attempt checkpoints, and postflight are
out of scope here (M09-02/M09-04/M09-05); this module never mutates Git
state, never retries, and knows nothing about OpenCode.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from opencode_tools.domain import (
    ProcessResult,
    ProcessSpec,
    RunOutcome,
    TargetRepository,
    Workspace,
)
from opencode_tools.errors import PreflightError
from opencode_tools.ports import LogChannel, ProcessRunner

# Versioned defensive buffer for one Git utility call's stdout; mirrors
# opencode.py's own bound (System Design SS10.1) but is not shared with it --
# this module must not know about OpenCode (System Design SS6).
_UTILITY_OUTPUT_LIMIT_BYTES = 1_048_576

# The exact, read-only preflight sequence (System Design SS11.1). This is the
# only set of Git commands git_safety.py is ever allowed to construct.
_CMD_SHOW_TOPLEVEL: tuple[str, ...] = ("rev-parse", "--show-toplevel")
_CMD_IS_BARE: tuple[str, ...] = ("rev-parse", "--is-bare-repository")
_CMD_ABSOLUTE_GIT_DIR: tuple[str, ...] = ("rev-parse", "--absolute-git-dir")
_CMD_BRANCH_SHOW_CURRENT: tuple[str, ...] = ("branch", "--show-current")
_CMD_HEAD_VERIFY: tuple[str, ...] = ("rev-parse", "--verify", "HEAD^{commit}")
_CMD_STATUS: tuple[str, ...] = (
    "status",
    "--porcelain=v1",
    "-z",
    "--untracked-files=all",
)

ALLOWED_GIT_ARGV_TAILS: tuple[tuple[str, ...], ...] = (
    _CMD_SHOW_TOPLEVEL,
    _CMD_IS_BARE,
    _CMD_ABSOLUTE_GIT_DIR,
    _CMD_BRANCH_SHOW_CURRENT,
    _CMD_HEAD_VERIFY,
    _CMD_STATUS,
)


class _CapturingSink:
    """An in-memory `AttemptLogSink` for one Git utility call's stdout.

    Mirrors `opencode.py`'s `_BoundedCapturingSink` in shape; duplicated
    rather than imported because this module must not know about OpenCode
    (System Design SS6 module table). Only `stdout` is retained -- these
    probes are classified by `ProcessResult.outcome` and their stdout text,
    never by parsing stderr.
    """

    def __init__(self, *, path: Path, max_bytes: int) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._overflowed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        del timestamp
        if channel != "stdout" or self._overflowed:
            return
        if len(self._buffer) + len(payload) > self._max_bytes:
            self._overflowed = True
            return
        self._buffer.extend(payload)

    def close(self) -> None:
        return None

    @property
    def overflowed_stdout(self) -> bool:
        return self._overflowed

    def stdout_bytes(self) -> bytes:
        return bytes(self._buffer)


def resolve_git_executable() -> Path:
    """Resolve the `git` executable once via `PATH` (ADR-009).

    Never tries an alias or an automatic install; a missing executable fails
    closed immediately.
    """

    found = shutil.which("git")
    if found is None:
        raise PreflightError(
            "git_safety.executable_not_found",
            "The 'git' executable was not found on PATH.",
        )
    return Path(found).resolve()


def check_git_argv_is_allowlisted(argv_tail: tuple[str, ...]) -> None:
    """Assert `argv_tail` is one of the fixed read-only preflight commands.

    Self-verification of this module's own command construction (mirrors
    `opencode.py`'s `check_no_forbidden_flags`): a mismatch here would be a
    bug in `git_safety.py` itself, never a fact about the target repository,
    so it raises the plain `AssertionError` a genuinely impossible internal
    invariant deserves.
    """

    if argv_tail not in ALLOWED_GIT_ARGV_TAILS:
        raise AssertionError(
            f"git_safety.py constructed a non-allowlisted git command: {argv_tail}"
        )


def build_git_argv(
    git_executable: Path, target: Path, argv_tail: tuple[str, ...]
) -> tuple[str, ...]:
    """Build one `<git> -C <target> <argv_tail>` invocation.

    `-C <target>` is always explicit; no command built by this module ever
    relies on the child's `cwd` to select which repository it operates on
    (System Design SS11.1).
    """

    check_git_argv_is_allowlisted(argv_tail)
    return (str(git_executable), "-C", str(target), *argv_tail)


def _run_git_probe(
    process_runner: ProcessRunner,
    git_executable: Path,
    target: Path,
    argv_tail: tuple[str, ...],
    *,
    log_name: str,
    cwd: Path,
    timeout_seconds: float,
    termination_grace_seconds: float,
) -> tuple[ProcessResult, _CapturingSink]:
    spec = ProcessSpec(
        argv=build_git_argv(git_executable, target, argv_tail),
        cwd=cwd,
        stdin=None,
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    sink = _CapturingSink(path=Path(log_name), max_bytes=_UTILITY_OUTPUT_LIMIT_BYTES)
    result = process_runner.run(spec, sink=sink)
    return result, sink


def _require_probe_succeeded(result: ProcessResult, *, code: str, message: str) -> None:
    if result.outcome is not RunOutcome.SUCCEEDED:
        raise PreflightError(
            code,
            message,
            technical_detail=(
                f"outcome={result.outcome.value} return_code={result.return_code}"
            ),
        )


def _decode_probe_stdout(sink: _CapturingSink, *, code: str) -> str:
    if sink.overflowed_stdout:
        raise PreflightError(
            code, "Git probe output exceeded the defensive size limit."
        )
    try:
        return sink.stdout_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PreflightError(code, "Git probe output was not valid UTF-8.") from None


def check_top_level(raw_output: str, expected_target: Path) -> None:
    """Reject a target whose Git top-level differs from `expected_target`.

    Covers a target outside the workspace, a symlink-escaped target, a
    non-top-level (nested) target, and a target that is not a Git working
    tree at all (AC-004).
    """

    reported = raw_output.removesuffix("\n")
    if reported != str(expected_target):
        raise PreflightError(
            "git_safety.not_git_top_level",
            "The target is not the Git top-level of its own working tree.",
            technical_detail=f"reported={reported!r} expected={str(expected_target)!r}",
        )


def check_not_bare(raw_output: str) -> None:
    """Reject a bare repository, which has no working tree (AC-006)."""

    reported = raw_output.removesuffix("\n")
    if reported != "false":
        raise PreflightError(
            "git_safety.bare_repository",
            "The target is a bare Git repository.",
            technical_detail=f"reported={reported!r}",
        )


def parse_git_common_dir(raw_output: str) -> Path:
    """Parse `git rev-parse --absolute-git-dir`'s output into a `Path`."""

    reported = raw_output.removesuffix("\n")
    if not reported:
        raise PreflightError(
            "git_safety.git_dir_probe_failed",
            "git rev-parse --absolute-git-dir returned no output.",
        )
    return Path(reported)


def check_branch_attached(raw_output: str) -> str:
    """Reject a detached `HEAD`, and return the current branch name.

    `git branch --show-current` prints nothing at all when `HEAD` is
    detached (AC-006).
    """

    branch = raw_output.removesuffix("\n")
    if not branch:
        raise PreflightError(
            "git_safety.detached_head",
            "The target has a detached HEAD; an attached branch is required.",
        )
    return branch


def check_clean_worktree(raw_output: str) -> None:
    """Reject any staged, unstaged, or untracked (non-ignored) change.

    `git status --porcelain=v1 -z --untracked-files=all` prints nothing at
    all for a clean working tree; ignored files are excluded by default, so
    no `--ignored` handling is needed (AC-005).
    """

    if raw_output:
        raise PreflightError(
            "git_safety.dirty_worktree",
            "The target has staged, unstaged, or untracked changes.",
        )


def resolve_target(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    workspace: Workspace,
    target_root: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> TargetRepository:
    """Run the full read-only Git preflight and return the proven target.

    In System Design SS11.1 order: containment after symlink resolution,
    Git top-level equality, non-bare, attached branch, resolvable `HEAD`,
    and a clean working tree. Every probe shares `utility_timeout_seconds`;
    a timeout, non-zero exit, or undecodable/ambiguous output on any of them
    fails closed with `PreflightError`, exactly like an outright rejection
    -- an incomplete check is never treated as clean (AC-004-AC-006,
    AC-036). Nothing here ever calls `os.chdir()`; every command addresses
    `target_root` explicitly via `-C`, and `cwd` is fixed to
    `workspace.root` regardless.
    """

    try:
        resolved_target = target_root.resolve(strict=True)
    except OSError as error:
        raise PreflightError(
            "git_safety.target_not_found",
            "The target path does not exist or could not be resolved.",
            technical_detail=str(error),
        ) from None

    if not resolved_target.is_relative_to(workspace.root):
        raise PreflightError(
            "git_safety.target_escapes_workspace",
            "The target escapes the workspace after symlink resolution.",
            technical_detail=f"target={resolved_target} workspace={workspace.root}",
        )

    def _probe(
        argv_tail: tuple[str, ...], *, log_name: str
    ) -> tuple[ProcessResult, _CapturingSink]:
        return _run_git_probe(
            process_runner,
            git_executable,
            resolved_target,
            argv_tail,
            log_name=log_name,
            cwd=workspace.root,
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
        )

    toplevel_result, toplevel_sink = _probe(
        _CMD_SHOW_TOPLEVEL, log_name="git-preflight-toplevel.log"
    )
    _require_probe_succeeded(
        toplevel_result,
        code="git_safety.top_level_probe_failed",
        message=(
            "git rev-parse --show-toplevel did not complete successfully; the "
            "target may not be a Git working tree."
        ),
    )
    check_top_level(
        _decode_probe_stdout(toplevel_sink, code="git_safety.top_level_probe_failed"),
        resolved_target,
    )

    bare_result, bare_sink = _probe(_CMD_IS_BARE, log_name="git-preflight-bare.log")
    _require_probe_succeeded(
        bare_result,
        code="git_safety.bare_probe_failed",
        message="git rev-parse --is-bare-repository did not complete successfully.",
    )
    check_not_bare(_decode_probe_stdout(bare_sink, code="git_safety.bare_probe_failed"))

    git_dir_result, git_dir_sink = _probe(
        _CMD_ABSOLUTE_GIT_DIR, log_name="git-preflight-git-dir.log"
    )
    _require_probe_succeeded(
        git_dir_result,
        code="git_safety.git_dir_probe_failed",
        message="git rev-parse --absolute-git-dir did not complete successfully.",
    )
    git_common_dir = parse_git_common_dir(
        _decode_probe_stdout(git_dir_sink, code="git_safety.git_dir_probe_failed")
    )

    branch_result, branch_sink = _probe(
        _CMD_BRANCH_SHOW_CURRENT, log_name="git-preflight-branch.log"
    )
    _require_probe_succeeded(
        branch_result,
        code="git_safety.branch_probe_failed",
        message="git branch --show-current did not complete successfully.",
    )
    check_branch_attached(
        _decode_probe_stdout(branch_sink, code="git_safety.branch_probe_failed")
    )

    head_result, _head_sink = _probe(
        _CMD_HEAD_VERIFY, log_name="git-preflight-head.log"
    )
    _require_probe_succeeded(
        head_result,
        code="git_safety.head_not_resolvable",
        message=(
            "git rev-parse --verify HEAD^{commit} did not complete successfully "
            "(unborn branch or unresolvable HEAD)."
        ),
    )

    status_result, status_sink = _probe(
        _CMD_STATUS, log_name="git-preflight-status.log"
    )
    _require_probe_succeeded(
        status_result,
        code="git_safety.status_probe_failed",
        message="git status did not complete successfully.",
    )
    check_clean_worktree(
        _decode_probe_stdout(status_sink, code="git_safety.status_probe_failed")
    )

    return TargetRepository(
        root=resolved_target,
        workspace_relative=resolved_target.relative_to(workspace.root),
        git_common_dir=git_common_dir,
    )


__all__ = (
    "ALLOWED_GIT_ARGV_TAILS",
    "build_git_argv",
    "check_branch_attached",
    "check_clean_worktree",
    "check_git_argv_is_allowlisted",
    "check_not_bare",
    "check_top_level",
    "parse_git_common_dir",
    "resolve_git_executable",
    "resolve_target",
)

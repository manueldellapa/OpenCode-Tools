"""Disposable coder working-tree isolation for Git metadata safety.

The coder must never execute inside the real target repository.  This module
creates an independent local clone, mirrors the target's current uncommitted
working-tree state into it, records that state as a sandbox-only baseline
commit, and later promotes only the coder's delta back into the real target.

The clone uses its own object database (no hard links) and has every remote
removed before OpenCode sees it.  Deleting or reinitializing the sandbox
`.git` therefore cannot destroy the real target's refs, objects, reflog,
index, config, or hooks (GitHub issue #90).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from opencode_tools import git_safety
from opencode_tools.domain import (
    GitSafetyStatus,
    GitState,
    ProcessResult,
    ProcessSpec,
    RunOutcome,
    TargetRepository,
)
from opencode_tools.ports import Clock, LogChannel, ProcessRunner

_MAX_CAPTURE_BYTES = 64 * 1024 * 1024
_SANDBOX_COMMIT_MESSAGE = "OpenCode-Tools coder sandbox baseline"
_SANDBOX_AUTHOR_NAME = "OpenCode-Tools"
_SANDBOX_AUTHOR_EMAIL = "opencode-tools@localhost"


class CoderSandboxError(Exception):
    """A fail-closed sandbox preparation, validation, or promotion error."""

    def __init__(
        self, message: str, *, process_result: ProcessResult | None = None
    ) -> None:
        super().__init__(message)
        self.process_result = process_result


class _MemorySink:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._overflowed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        del timestamp
        target = self._stdout if channel == "stdout" else self._stderr
        remaining = _MAX_CAPTURE_BYTES - len(target)
        if remaining <= 0:
            self._overflowed = True
            return
        target.extend(payload[:remaining])
        if len(payload) > remaining:
            self._overflowed = True

    def close(self) -> None:
        return

    @property
    def stdout(self) -> bytes:
        return bytes(self._stdout)

    @property
    def overflowed(self) -> bool:
        return self._overflowed


@dataclass(frozen=True, slots=True)
class CoderSandbox:
    container_root: Path
    root: Path
    baseline_head: str
    target_state: GitState

    def __post_init__(self) -> None:
        if not self.container_root.is_absolute():
            raise ValueError("container_root must be absolute")
        if not self.root.is_absolute():
            raise ValueError("root must be absolute")
        if not self.baseline_head:
            raise ValueError("baseline_head must not be empty")
        if not isinstance(self.target_state, GitState):
            raise TypeError("target_state must be GitState")


def _run_git(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    cwd: Path,
    argv_tail: tuple[str, ...],
    timeout_seconds: float,
    termination_grace_seconds: float,
    stdin: bytes | None = None,
    environment_overrides: dict[str, str] | None = None,
    log_name: str,
) -> tuple[ProcessResult, bytes]:
    sink = _MemorySink(Path(log_name))
    spec = ProcessSpec(
        argv=(str(git_executable), *argv_tail),
        cwd=cwd,
        stdin=stdin,
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        environment_overrides={
            "GIT_TERMINAL_PROMPT": "0",
            **(environment_overrides or {}),
        },
    )
    result = process_runner.run(spec, sink=sink)
    if sink.overflowed:
        raise CoderSandboxError(
            "A trusted Git sandbox command exceeded the bounded capture size.",
            process_result=result,
        )
    if result.outcome is not RunOutcome.SUCCEEDED:
        raise CoderSandboxError(
            "A trusted Git sandbox command did not complete successfully.",
            process_result=result,
        )
    return result, sink.stdout


def _single_line(payload: bytes, *, field_name: str) -> str:
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise CoderSandboxError(
            f"Sandbox {field_name} output was not valid UTF-8."
        ) from error
    if not text or "\n" in text or "\r" in text:
        raise CoderSandboxError(
            f"Sandbox {field_name} output was missing or ambiguous."
        )
    return text


def prepare_coder_sandbox(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    target: TargetRepository,
    clock: Clock,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> CoderSandbox:
    """Create an independent clone representing the target's current state."""

    capture = git_safety.capture_git_state(
        process_runner,
        git_executable=git_executable,
        target=target,
        clock=clock,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    if (
        capture.safety_status is not GitSafetyStatus.SAFE
        or capture.state.fingerprint is None
        or capture.state.head is None
        or capture.state.branch is None
    ):
        raise CoderSandboxError(
            "The real target could not be snapshotted safely before sandboxing."
        )

    container_root = Path(
        tempfile.mkdtemp(prefix="opencode-tools-coder-sandbox-")
    ).resolve()
    try:
        os.chmod(container_root, 0o700)
        sandbox_root = container_root / "target"

        _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=container_root,
            argv_tail=(
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                str(target.root),
                str(sandbox_root),
            ),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            log_name="coder-sandbox-clone.log",
        )

        _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=sandbox_root,
            argv_tail=("-C", str(sandbox_root), "remote", "remove", "origin"),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            log_name="coder-sandbox-remove-origin.log",
        )

        git_dir = sandbox_root / ".git"
        if not git_dir.is_dir() or git_dir.is_symlink():
            raise CoderSandboxError(
                "The coder sandbox does not own an independent .git directory."
            )
        if (git_dir / "objects" / "info" / "alternates").exists():
            raise CoderSandboxError(
                "The coder sandbox unexpectedly references an alternate object store."
            )

        source_index = container_root / "source.index"
        source_index_env = {"GIT_INDEX_FILE": str(source_index)}
        _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=target.root,
            argv_tail=("-C", str(target.root), "read-tree", "HEAD"),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            environment_overrides=source_index_env,
            log_name="coder-sandbox-source-read-tree.log",
        )
        _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=target.root,
            argv_tail=("-C", str(target.root), "add", "-A", "--"),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            environment_overrides=source_index_env,
            log_name="coder-sandbox-source-add.log",
        )
        _, source_patch = _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=target.root,
            argv_tail=(
                "-C",
                str(target.root),
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "HEAD",
                "--",
            ),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            environment_overrides=source_index_env,
            log_name="coder-sandbox-source-diff.log",
        )
        if source_patch:
            _run_git(
                process_runner,
                git_executable=git_executable,
                cwd=sandbox_root,
                argv_tail=(
                    "-C",
                    str(sandbox_root),
                    "apply",
                    "--binary",
                    "--whitespace=nowarn",
                    "-",
                ),
                timeout_seconds=utility_timeout_seconds,
                termination_grace_seconds=termination_grace_seconds,
                stdin=source_patch,
                log_name="coder-sandbox-seed-apply.log",
            )

        _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=sandbox_root,
            argv_tail=("-C", str(sandbox_root), "add", "-A", "--"),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            log_name="coder-sandbox-baseline-add.log",
        )
        _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=sandbox_root,
            argv_tail=(
                "-C",
                str(sandbox_root),
                "-c",
                f"user.name={_SANDBOX_AUTHOR_NAME}",
                "-c",
                f"user.email={_SANDBOX_AUTHOR_EMAIL}",
                "-c",
                f"core.hooksPath={os.devnull}",
                "commit",
                "--quiet",
                "--allow-empty",
                "--no-gpg-sign",
                "-m",
                _SANDBOX_COMMIT_MESSAGE,
            ),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            log_name="coder-sandbox-baseline-commit.log",
        )
        _, baseline_head_raw = _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=sandbox_root,
            argv_tail=(
                "-C",
                str(sandbox_root),
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            log_name="coder-sandbox-baseline-head.log",
        )
        baseline_head = _single_line(baseline_head_raw, field_name="baseline HEAD")

        return CoderSandbox(
            container_root=container_root,
            root=sandbox_root,
            baseline_head=baseline_head,
            target_state=capture.state,
        )
    except Exception:
        shutil.rmtree(container_root, ignore_errors=True)
        raise


def promote_coder_changes(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    target: TargetRepository,
    sandbox: CoderSandbox,
    clock: Clock,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> None:
    """Promote only the sandbox delta when both repositories are trustworthy."""

    git_dir = sandbox.root / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise CoderSandboxError(
            "The coder damaged or replaced the sandbox Git metadata; changes were discarded."
        )

    _, top_raw = _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=sandbox.root,
        argv_tail=("-C", str(sandbox.root), "rev-parse", "--show-toplevel"),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        log_name="coder-sandbox-verify-toplevel.log",
    )
    try:
        top = Path(_single_line(top_raw, field_name="top-level")).resolve(strict=True)
    except OSError as error:
        raise CoderSandboxError(
            "The coder sandbox top-level could not be resolved."
        ) from error
    if top != sandbox.root:
        raise CoderSandboxError("The coder sandbox Git top-level changed unexpectedly.")

    try:
        _, head_raw = _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=sandbox.root,
            argv_tail=(
                "-C",
                str(sandbox.root),
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            log_name="coder-sandbox-verify-head.log",
        )
        sandbox_head = _single_line(head_raw, field_name="HEAD")
    except CoderSandboxError as error:
        raise CoderSandboxError(
            "The coder sandbox HEAD is missing or invalid; no changes were promoted.",
            process_result=error.process_result,
        ) from None
    if sandbox_head != sandbox.baseline_head:
        raise CoderSandboxError(
            "The coder changed sandbox HEAD; no changes were promoted."
        )

    target_capture = git_safety.capture_git_state(
        process_runner,
        git_executable=git_executable,
        target=target,
        clock=clock,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    baseline = sandbox.target_state
    current = target_capture.state
    if (
        target_capture.safety_status is not GitSafetyStatus.SAFE
        or current.branch != baseline.branch
        or current.head != baseline.head
        or current.fingerprint != baseline.fingerprint
    ):
        raise CoderSandboxError(
            "The real target changed while the coder was running; sandbox changes were not promoted."
        )

    _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=sandbox.root,
        argv_tail=("-C", str(sandbox.root), "add", "-A", "--"),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        log_name="coder-sandbox-promote-add.log",
    )
    _, patch = _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=sandbox.root,
        argv_tail=(
            "-C",
            str(sandbox.root),
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "HEAD",
            "--",
        ),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        log_name="coder-sandbox-promote-diff.log",
    )
    if not patch:
        return

    _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=target.root,
        argv_tail=(
            "-C",
            str(target.root),
            "apply",
            "--binary",
            "--whitespace=nowarn",
            "-",
        ),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        stdin=patch,
        log_name="coder-sandbox-promote-apply.log",
    )


def cleanup_coder_sandbox(sandbox: CoderSandbox) -> None:
    shutil.rmtree(sandbox.container_root, ignore_errors=True)


__all__ = (
    "CoderSandbox",
    "CoderSandboxError",
    "cleanup_coder_sandbox",
    "prepare_coder_sandbox",
    "promote_coder_changes",
)

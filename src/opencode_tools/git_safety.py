"""Read-only Git target preflight and fingerprint (System Design SS11.1-11.2;
ADR-003/004/009/010).

This module proves, before any agent runs, that a target repository is safe
to operate on: contained in the workspace after symlink resolution, the Git
top-level of a non-bare repository, on an attached branch with a resolvable
`HEAD`, and free of staged, unstaged, or untracked changes. Every probe is
read-only, addresses the target explicitly with `git -C <target>` -- never
an implicit `cwd`/workspace -- and shares one bounded `utility_timeout_seconds`
deadline. An ambiguous, timed-out, or non-zero probe result fails closed
rather than being treated as clean (AC-004-AC-006, AC-036).

It also computes the `git-state-v1` content-sensitive fingerprint: a
versioned SHA-256 of length-prefixed records covering raw porcelain, the
index manifest, tracked/untracked path lists, and -- read directly from
disk, not from Git's own object database -- each path's type, executable
bit, and content/symlink-target hash, so a second edit to an already-`M`
file changes the digest even though the porcelain line does not (M09-02).

`capture_git_state` wraps that fingerprint acquisition to be bounded and
fail-closed under concurrent or incomplete reads (M09-03): every per-path
read is lstat-bracketed, the whole snapshot is resampled once at the end,
a single instability triggers at most one bounded retry, and a repeated
instability, permission error, escaping path, unsupported file type, or
probe failure/ambiguity is classified `GitSafetyStatus.INDETERMINATE`
rather than ever guessed `SAFE`.

Per-attempt checkpoints, role mutation policy, change inventory, and
postflight are out of scope here (M09-04/M09-05); this module never
mutates Git state, never retries beyond the one bounded resample, and
knows nothing about OpenCode.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from opencode_tools.domain import (
    GitSafetyStatus,
    GitState,
    ProcessResult,
    ProcessSpec,
    RunOutcome,
    TargetRepository,
    Workspace,
)
from opencode_tools.errors import PreflightError
from opencode_tools.ports import Clock, LogChannel, ProcessRunner
from opencode_tools.process import deadline_ns

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
_CMD_LS_FILES_STAGE: tuple[str, ...] = ("ls-files", "--stage", "-z")
_CMD_LS_FILES: tuple[str, ...] = ("ls-files", "-z")
_CMD_LS_FILES_OTHERS: tuple[str, ...] = (
    "ls-files",
    "--others",
    "--exclude-standard",
    "-z",
)

ALLOWED_GIT_ARGV_TAILS: tuple[tuple[str, ...], ...] = (
    _CMD_SHOW_TOPLEVEL,
    _CMD_IS_BARE,
    _CMD_ABSOLUTE_GIT_DIR,
    _CMD_BRANCH_SHOW_CURRENT,
    _CMD_HEAD_VERIFY,
    _CMD_STATUS,
    _CMD_LS_FILES_STAGE,
    _CMD_LS_FILES,
    _CMD_LS_FILES_OTHERS,
)

# System Design SS11.2's versioned fingerprint algorithm tag; matches
# `GitState.fingerprint_version`'s frozen default in domain.py exactly.
GIT_STATE_FINGERPRINT_VERSION = "git-state-v1"

# The Git index mode that marks a path as a gitlink (submodule reference)
# rather than a regular blob (System Design SS11.2 point 6).
_GITLINK_MODE = "160000"

# Big-endian byte width of each length-prefix header in the fingerprint's
# serialization; 8 bytes is far beyond any real record's size but keeps the
# framing trivially unambiguous.
_LENGTH_PREFIX_BYTES = 8


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


def _raw_probe_stdout(sink: _CapturingSink, *, code: str) -> bytes:
    """Return one probe's raw stdout bytes, undecoded.

    Fingerprint evidence is never UTF-8-decoded (System Design SS11.2's
    byte-safe raw paths): unlike `_decode_probe_stdout`, this only enforces
    the defensive size bound.
    """

    if sink.overflowed_stdout:
        raise PreflightError(
            code, "Git probe output exceeded the defensive size limit."
        )
    return sink.stdout_bytes()


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


# =============================================================================
# M09-02: `git-state-v1` content-sensitive fingerprint
# =============================================================================


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One `git ls-files --stage -z` record: a path's registered index state."""

    mode: str
    object_id: str
    stage: int
    path: bytes


@dataclass(frozen=True, slots=True)
class FingerprintPathEntry:
    """One tracked or untracked path's contribution to the fingerprint.

    `content_hash` is the SHA-256 digest of the file's raw on-disk content
    (`type == "regular"`) or of its raw symlink target (`type == "symlink"`);
    it is `None` for `"missing"` (listed by Git but absent on disk) and
    `"other"` (not a regular file or symlink -- a FIFO, socket, device, or
    similar) paths, which contribute only their path and type.
    """

    path: bytes
    type: str
    executable: bool
    content_hash: bytes | None


@dataclass(frozen=True, slots=True)
class FingerprintGitlinkEntry:
    """One gitlink (submodule reference)'s contribution to the fingerprint.

    Only the index's own recorded object ID is included; the submodule's
    own working-tree state is never inspected (System Design SS11.2 point
    6) -- the parent repository's porcelain output, already part of the
    fingerprint, already carries whether the gitlink path itself is dirty.
    """

    path: bytes
    object_id: str


def split_null_terminated_records(raw: bytes) -> tuple[bytes, ...]:
    """Split `-z`-terminated Git output into its raw per-record entries.

    Every record produced by `-z` output ends with `NUL`, including the
    last one, so a trailing empty split segment is always dropped rather
    than treated as a record.
    """

    if not raw:
        return ()
    return tuple(raw.split(b"\x00")[:-1])


def parse_index_manifest(raw: bytes) -> tuple[IndexEntry, ...]:
    """Parse `git ls-files --stage -z` output into structured `IndexEntry`s.

    Each record is `<mode> <object-id> <stage>\\t<path>`; only the first tab
    separates the header from the raw path, so a path byte-for-byte
    containing a tab is still parsed correctly.
    """

    entries = []
    for record in split_null_terminated_records(raw):
        header, separator, path = record.partition(b"\t")
        if not separator:
            raise PreflightError(
                "git_safety.fingerprint_index_malformed",
                "git ls-files --stage -z produced a record with no path.",
                technical_detail=f"record={record!r}",
            )
        mode_bytes, _, remainder = header.partition(b" ")
        object_id_bytes, _, stage_bytes = remainder.partition(b" ")
        try:
            stage = int(stage_bytes)
        except ValueError:
            raise PreflightError(
                "git_safety.fingerprint_index_malformed",
                "git ls-files --stage -z produced a record with a non-numeric stage.",
                technical_detail=f"record={record!r}",
            ) from None
        entries.append(
            IndexEntry(
                mode=mode_bytes.decode("ascii"),
                object_id=object_id_bytes.decode("ascii"),
                stage=stage,
                path=path,
            )
        )
    return tuple(entries)


def classify_lstat_mode(st_mode: int) -> str:
    """Classify an `lstat` mode as `"regular"`, `"symlink"`, or `"other"`.

    Never returns `"missing"`: that classification applies only when
    `lstat` itself finds nothing there, which this pure function -- given
    an already-obtained mode -- cannot observe (System Design SS11.2).
    """

    if stat.S_ISREG(st_mode):
        return "regular"
    if stat.S_ISLNK(st_mode):
        return "symlink"
    return "other"


def _length_prefixed(data: bytes) -> bytes:
    return len(data).to_bytes(_LENGTH_PREFIX_BYTES, "big") + data


def build_git_state_fingerprint(
    *,
    porcelain_raw: bytes,
    index_manifest_raw: bytes,
    tracked_raw: bytes,
    untracked_raw: bytes,
    path_entries: tuple[FingerprintPathEntry, ...],
    gitlink_entries: tuple[FingerprintGitlinkEntry, ...],
) -> str:
    """Deterministically assemble the `git-state-v1` SHA-256 hex digest.

    Every field -- the four raw command outputs and every per-path/gitlink
    record -- is individually length-prefixed, so the digest never depends
    on adjacent field lengths creating an ambiguous byte boundary. The two
    per-path record sets are sorted by raw path before hashing, so the
    result never depends on the caller's own ordering or on dict/set
    iteration order (System Design SS11.2).
    """

    digest = hashlib.sha256()
    digest.update(_length_prefixed(GIT_STATE_FINGERPRINT_VERSION.encode("ascii")))
    digest.update(_length_prefixed(porcelain_raw))
    digest.update(_length_prefixed(index_manifest_raw))
    digest.update(_length_prefixed(tracked_raw))
    digest.update(_length_prefixed(untracked_raw))

    for entry in sorted(path_entries, key=lambda item: item.path):
        digest.update(_length_prefixed(entry.path))
        digest.update(_length_prefixed(entry.type.encode("ascii")))
        digest.update(_length_prefixed(b"\x01" if entry.executable else b"\x00"))
        digest.update(_length_prefixed(entry.content_hash or b""))

    for gitlink in sorted(gitlink_entries, key=lambda item: item.path):
        digest.update(_length_prefixed(gitlink.path))
        digest.update(_length_prefixed(b"gitlink"))
        digest.update(_length_prefixed(gitlink.object_id.encode("ascii")))

    return digest.hexdigest()


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PreflightError(
            "git_safety.fingerprint_path_unreadable",
            "A tracked or untracked path could not be inspected.",
            technical_detail=f"path={path} error={error}",
        ) from None


def _read_file_or_raise(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise PreflightError(
            "git_safety.fingerprint_path_unreadable",
            "A tracked or untracked regular file could not be read.",
            technical_detail=f"path={path} error={error}",
        ) from None


def _readlink_or_raise(path: Path) -> bytes:
    try:
        return os.readlink(os.fsencode(str(path)))
    except OSError as error:
        raise PreflightError(
            "git_safety.fingerprint_path_unreadable",
            "A tracked or untracked symlink could not be read.",
            technical_detail=f"path={path} error={error}",
        ) from None


def build_fingerprint_path_entry(
    target_root: Path, raw_path: bytes
) -> FingerprintPathEntry:
    """Classify one tracked or untracked path and hash its on-disk state.

    `raw_path` is `target_root`-relative, exactly as Git printed it;
    `os.fsdecode` round-trips it byte-for-byte via `surrogateescape`, so a
    non-UTF-8 path is handled without ever raising a decode error. A path
    escaping `target_root` (a `..` component -- Git itself never emits one,
    but this is checked defensively) raises `PreflightError` rather than
    being joined and inspected. Otherwise the result is `"missing"` if
    nothing exists there, `"regular"`/`"symlink"` with a content/target
    hash and (for `"regular"`) the Git-significant executable bit, or
    `"other"` for anything else (System Design SS11.2).
    """

    relative = os.fsdecode(raw_path)
    if ".." in Path(relative).parts:
        raise PreflightError(
            "git_safety.fingerprint_path_escapes_target",
            "A tracked or untracked path escapes the target.",
            technical_detail=f"path={raw_path!r}",
        )
    absolute = target_root / relative
    lstat_result = _lstat_or_none(absolute)
    if lstat_result is None:
        return FingerprintPathEntry(
            path=raw_path, type="missing", executable=False, content_hash=None
        )

    path_type = classify_lstat_mode(lstat_result.st_mode)
    if path_type == "regular":
        content_hash = hashlib.sha256(_read_file_or_raise(absolute)).digest()
        executable = bool(lstat_result.st_mode & stat.S_IXUSR)
        return FingerprintPathEntry(
            path=raw_path,
            type=path_type,
            executable=executable,
            content_hash=content_hash,
        )
    if path_type == "symlink":
        content_hash = hashlib.sha256(_readlink_or_raise(absolute)).digest()
        return FingerprintPathEntry(
            path=raw_path, type=path_type, executable=False, content_hash=content_hash
        )
    return FingerprintPathEntry(
        path=raw_path, type=path_type, executable=False, content_hash=None
    )


def compute_git_state_fingerprint(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    target: TargetRepository,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> str:
    """Compute the `git-state-v1` fingerprint for `target`'s current state.

    Runs the four raw-evidence probes (porcelain status, index manifest,
    tracked list, untracked list) and, for every tracked or untracked path
    that is not a gitlink, reads its on-disk content or symlink target
    directly -- not through Git -- so a second edit to an already-`M` file
    changes the digest even though the porcelain line does not. Race and
    read-stability handling, and mapping a failure to `GitSafetyStatus`,
    are out of scope here (M09-03): a probe or read failure fails closed
    with `PreflightError`.
    """

    def _probe(
        argv_tail: tuple[str, ...], *, log_name: str
    ) -> tuple[ProcessResult, _CapturingSink]:
        return _run_git_probe(
            process_runner,
            git_executable,
            target.root,
            argv_tail,
            log_name=log_name,
            cwd=target.root,
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
        )

    porcelain_result, porcelain_sink = _probe(
        _CMD_STATUS, log_name="git-fingerprint-status.log"
    )
    _require_probe_succeeded(
        porcelain_result,
        code="git_safety.fingerprint_status_probe_failed",
        message="git status did not complete successfully.",
    )
    porcelain_raw = _raw_probe_stdout(
        porcelain_sink, code="git_safety.fingerprint_status_probe_failed"
    )

    index_result, index_sink = _probe(
        _CMD_LS_FILES_STAGE, log_name="git-fingerprint-index.log"
    )
    _require_probe_succeeded(
        index_result,
        code="git_safety.fingerprint_index_probe_failed",
        message="git ls-files --stage did not complete successfully.",
    )
    index_manifest_raw = _raw_probe_stdout(
        index_sink, code="git_safety.fingerprint_index_probe_failed"
    )

    tracked_result, tracked_sink = _probe(
        _CMD_LS_FILES, log_name="git-fingerprint-tracked.log"
    )
    _require_probe_succeeded(
        tracked_result,
        code="git_safety.fingerprint_tracked_probe_failed",
        message="git ls-files did not complete successfully.",
    )
    tracked_raw = _raw_probe_stdout(
        tracked_sink, code="git_safety.fingerprint_tracked_probe_failed"
    )

    untracked_result, untracked_sink = _probe(
        _CMD_LS_FILES_OTHERS, log_name="git-fingerprint-untracked.log"
    )
    _require_probe_succeeded(
        untracked_result,
        code="git_safety.fingerprint_untracked_probe_failed",
        message="git ls-files --others --exclude-standard did not complete "
        "successfully.",
    )
    untracked_raw = _raw_probe_stdout(
        untracked_sink, code="git_safety.fingerprint_untracked_probe_failed"
    )

    index_entries = parse_index_manifest(index_manifest_raw)
    gitlink_paths = frozenset(
        entry.path for entry in index_entries if entry.mode == _GITLINK_MODE
    )
    gitlink_entries = tuple(
        FingerprintGitlinkEntry(path=entry.path, object_id=entry.object_id)
        for entry in index_entries
        if entry.mode == _GITLINK_MODE
    )

    all_paths = frozenset(split_null_terminated_records(tracked_raw)) | frozenset(
        split_null_terminated_records(untracked_raw)
    )
    path_entries = tuple(
        build_fingerprint_path_entry(target.root, raw_path)
        for raw_path in all_paths
        if raw_path not in gitlink_paths
    )

    return build_git_state_fingerprint(
        porcelain_raw=porcelain_raw,
        index_manifest_raw=index_manifest_raw,
        tracked_raw=tracked_raw,
        untracked_raw=untracked_raw,
        path_entries=path_entries,
        gitlink_entries=gitlink_entries,
    )


# =============================================================================
# M09-03: bounded, fail-closed sampling stability and `GitSafetyStatus`
# =============================================================================


@dataclass(frozen=True, slots=True)
class GitStateCapture:
    """One classified Git state snapshot.

    `safety_status` here means only "was this capture itself acquired
    reliably" -- `SAFE` for a stable, complete sample, `INDETERMINATE` for
    anything unstable, erroring, or ambiguous. It is never `UNSAFE`: that
    verdict requires comparing this capture against a baseline or a prior
    checkpoint (branch/HEAD drift, an unexpected fingerprint delta), which
    is M09-04's job, not this module's acquisition primitive. A `SAFE`
    capture can still report a detached `HEAD` (`state.branch is None`) --
    that is itself meaningful drift evidence for M09-04 to compare, not an
    acquisition failure.
    """

    state: GitState
    safety_status: GitSafetyStatus
    process_results: tuple[ProcessResult, ...]


# Codes from this module's own probes/reads that represent a condition a
# retry might resolve (an unstable read) or that always fails closed to
# INDETERMINATE (a permission error, an escaping path, an unsupported
# type, or a probe outright failing) -- never a bug in this module's own
# command construction, which raises AssertionError instead.
_INDETERMINATE_CAPTURE_CODES = frozenset(
    {
        "git_safety.state_branch_probe_failed",
        "git_safety.state_head_probe_failed",
        "git_safety.state_status_probe_failed",
        "git_safety.state_index_probe_failed",
        "git_safety.state_tracked_probe_failed",
        "git_safety.state_untracked_probe_failed",
        "git_safety.fingerprint_path_unreadable",
        "git_safety.fingerprint_path_escapes_target",
        "git_safety.fingerprint_index_malformed",
        "git_safety.fingerprint_unsupported_type",
    }
)


class _SnapshotUnstable(Exception):
    """Internal signal: this sampling pass raced (System Design SS11.2);
    the caller retries once, provided the shared deadline allows it."""

    def __init__(self, process_results: tuple[ProcessResult, ...]) -> None:
        super().__init__("git state snapshot was unstable")
        self.process_results = process_results


def capture_branch(raw_output: str) -> str | None:
    """Return the current branch name, or `None` for a detached `HEAD`.

    Unlike `check_branch_attached` (M09-01's bootstrap gate, which rejects a
    detached `HEAD` outright), this never raises: a detached `HEAD`
    captured mid-run is meaningful drift evidence for M09-04's baseline
    comparison, not by itself a capture failure.
    """

    return raw_output.removesuffix("\n") or None


def capture_head(raw_output: str) -> str:
    """Return the resolved `HEAD` commit SHA from a successful probe."""

    sha = raw_output.removesuffix("\n")
    if not sha:
        raise PreflightError(
            "git_safety.state_head_probe_failed",
            "git rev-parse --verify HEAD^{commit} succeeded but printed no output.",
        )
    return sha


def _display_safe_text(raw: bytes) -> str:
    """Decode `raw` for display/storage, never raising on non-UTF-8 bytes.

    Uses `backslashreplace`, not `surrogateescape`: the result must be
    valid Unicode -- it becomes a `GitState` `str` field that crosses the
    JSON boundary -- not merely round-trippable (System Design SS11.2's
    "forma display-safe").
    """

    return raw.decode("utf-8", errors="backslashreplace")


def _lstat_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        result = os.lstat(path)
    except OSError:
        return None
    return (result.st_ino, result.st_mtime_ns, result.st_size)


def _capture_path_entry(
    target_root: Path, raw_path: bytes
) -> tuple[FingerprintPathEntry, bool]:
    """Classify and hash one path, `lstat`-bracketed before and after.

    Returns `(entry, stable)`; `stable` is `False` if the path's identity
    (inode, mtime, size) changed during the read -- the per-read race
    signal System Design SS11.2 says to retry on rather than trust.
    """

    absolute = target_root / os.fsdecode(raw_path)
    before = _lstat_signature(absolute)
    entry = build_fingerprint_path_entry(target_root, raw_path)
    after = _lstat_signature(absolute)
    return entry, before == after


def _probe_raw(
    process_runner: ProcessRunner,
    git_executable: Path,
    target_root: Path,
    argv_tail: tuple[str, ...],
    *,
    log_name: str,
    code: str,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> tuple[bytes, ProcessResult]:
    result, sink = _run_git_probe(
        process_runner,
        git_executable,
        target_root,
        argv_tail,
        log_name=log_name,
        cwd=target_root,
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    _require_probe_succeeded(
        result,
        code=code,
        message=f"git {' '.join(argv_tail)} did not complete successfully.",
    )
    return _raw_probe_stdout(sink, code=code), result


_STATE_PROBES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("branch", _CMD_BRANCH_SHOW_CURRENT, "git_safety.state_branch_probe_failed"),
    ("head", _CMD_HEAD_VERIFY, "git_safety.state_head_probe_failed"),
    ("status", _CMD_STATUS, "git_safety.state_status_probe_failed"),
    ("index", _CMD_LS_FILES_STAGE, "git_safety.state_index_probe_failed"),
    ("tracked", _CMD_LS_FILES, "git_safety.state_tracked_probe_failed"),
    ("untracked", _CMD_LS_FILES_OTHERS, "git_safety.state_untracked_probe_failed"),
)


def _sample_six_raw_probes(
    process_runner: ProcessRunner,
    git_executable: Path,
    target_root: Path,
    *,
    log_prefix: str,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> tuple[dict[str, bytes], tuple[ProcessResult, ...]]:
    raw: dict[str, bytes] = {}
    results: list[ProcessResult] = []
    for key, argv_tail, code in _STATE_PROBES:
        raw[key], result = _probe_raw(
            process_runner,
            git_executable,
            target_root,
            argv_tail,
            log_name=f"{log_prefix}-{key}.log",
            code=code,
            utility_timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
        )
        results.append(result)
    return raw, tuple(results)


def _sample_git_state_once(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    target: TargetRepository,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
    attempt_index: int,
) -> tuple[GitState, tuple[ProcessResult, ...]]:
    """One full sample-then-resample pass.

    Raises `PreflightError` (a probe failure, an escaping path, an
    unreadable file, or an unsupported file type -- none of which a retry
    would resolve) or `_SnapshotUnstable` (an `lstat` or resample mismatch,
    which a retry might). Returns the completed `GitState` only when every
    signal -- every per-path `lstat` bracket and the whole-snapshot
    resample -- agrees.
    """

    log_prefix = f"git-state-{attempt_index}"
    first, first_results = _sample_six_raw_probes(
        process_runner,
        git_executable,
        target.root,
        log_prefix=f"{log_prefix}-a",
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )

    index_entries = parse_index_manifest(first["index"])
    gitlink_paths = frozenset(
        entry.path for entry in index_entries if entry.mode == _GITLINK_MODE
    )
    gitlink_entries = tuple(
        FingerprintGitlinkEntry(path=entry.path, object_id=entry.object_id)
        for entry in index_entries
        if entry.mode == _GITLINK_MODE
    )
    all_paths = frozenset(split_null_terminated_records(first["tracked"])) | frozenset(
        split_null_terminated_records(first["untracked"])
    )

    path_entries: list[FingerprintPathEntry] = []
    per_path_stable = True
    for raw_path in all_paths:
        if raw_path in gitlink_paths:
            continue
        entry, stable = _capture_path_entry(target.root, raw_path)
        if entry.type == "other":
            raise PreflightError(
                "git_safety.fingerprint_unsupported_type",
                "A tracked or untracked path is not a regular file or symlink.",
                technical_detail=f"path={raw_path!r}",
            )
        if entry.type == "missing" or not stable:
            per_path_stable = False
        path_entries.append(entry)

    second, second_results = _sample_six_raw_probes(
        process_runner,
        git_executable,
        target.root,
        log_prefix=f"{log_prefix}-b",
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    all_results = first_results + second_results

    if not per_path_stable or first != second:
        raise _SnapshotUnstable(all_results)

    fingerprint = build_git_state_fingerprint(
        porcelain_raw=first["status"],
        index_manifest_raw=first["index"],
        tracked_raw=first["tracked"],
        untracked_raw=first["untracked"],
        path_entries=tuple(path_entries),
        gitlink_entries=gitlink_entries,
    )
    state = GitState(
        root=target.root,
        branch=capture_branch(_display_safe_text(first["branch"])),
        head=capture_head(_display_safe_text(first["head"])),
        porcelain_summary=_display_safe_text(first["status"]),
        staged=(),
        unstaged=(),
        untracked=(),
        fingerprint=fingerprint,
    )
    return state, all_results


def _indeterminate_git_state(target_root: Path) -> GitState:
    return GitState(
        root=target_root,
        branch=None,
        head=None,
        porcelain_summary="",
        staged=(),
        unstaged=(),
        untracked=(),
        fingerprint=None,
    )


def capture_git_state(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    target: TargetRepository,
    clock: Clock,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> GitStateCapture:
    """Capture one bounded, fail-closed `git-state-v1` snapshot.

    At most two sampling passes share one `utility_timeout_seconds`
    deadline (System Design SS11.2, M09-03): a first instability -- a race
    detected via `lstat` bracketing or a manifest/status/branch/HEAD
    resample mismatch -- triggers exactly one retry, provided the deadline
    has not already passed; a second instability, a probe failure, a
    permission error, an escaping path, or an unsupported file type is
    classified `GitSafetyStatus.INDETERMINATE` rather than ever guessed
    `SAFE`. This never compares against a baseline or a prior checkpoint --
    that comparison, and any `UNSAFE` verdict it can produce, is M09-04's
    job.
    """

    start_ns = clock.monotonic_ns()
    deadline = deadline_ns(start_ns, utility_timeout_seconds)
    accumulated: tuple[ProcessResult, ...] = ()

    for attempt_index in range(2):
        if attempt_index > 0 and clock.monotonic_ns() >= deadline:
            break
        try:
            state, results = _sample_git_state_once(
                process_runner,
                git_executable=git_executable,
                target=target,
                utility_timeout_seconds=utility_timeout_seconds,
                termination_grace_seconds=termination_grace_seconds,
                attempt_index=attempt_index,
            )
        except _SnapshotUnstable as unstable:
            accumulated = accumulated + unstable.process_results
            continue
        except PreflightError as error:
            if error.code not in _INDETERMINATE_CAPTURE_CODES:
                raise
            return GitStateCapture(
                state=_indeterminate_git_state(target.root),
                safety_status=GitSafetyStatus.INDETERMINATE,
                process_results=accumulated,
            )
        return GitStateCapture(
            state=state,
            safety_status=GitSafetyStatus.SAFE,
            process_results=accumulated + results,
        )

    return GitStateCapture(
        state=_indeterminate_git_state(target.root),
        safety_status=GitSafetyStatus.INDETERMINATE,
        process_results=accumulated,
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
    "GIT_STATE_FINGERPRINT_VERSION",
    "FingerprintGitlinkEntry",
    "FingerprintPathEntry",
    "GitStateCapture",
    "IndexEntry",
    "build_fingerprint_path_entry",
    "build_git_argv",
    "build_git_state_fingerprint",
    "capture_branch",
    "capture_git_state",
    "capture_head",
    "check_branch_attached",
    "check_clean_worktree",
    "check_git_argv_is_allowlisted",
    "check_not_bare",
    "check_top_level",
    "classify_lstat_mode",
    "compute_git_state_fingerprint",
    "parse_git_common_dir",
    "parse_index_manifest",
    "resolve_git_executable",
    "resolve_target",
    "split_null_terminated_records",
)

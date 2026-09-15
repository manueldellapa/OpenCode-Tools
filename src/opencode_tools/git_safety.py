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

`check_git_state` (`GitSafetyPort.check`) compares one such capture against
the last accepted checkpoint and applies the role mutation policy (M09-04):
branch/HEAD drift is unsafe for any role; a fingerprint delta is expected
and safe for the coder but unsafe for every other role. It also reports
whether the target changed at all, the separate signal `retry.decide_retry`
uses to suppress a coder's provider retry.

Every checkpoint also carries a deterministic, display-safe change
inventory -- `GitState.staged`/`unstaged`/`untracked` -- parsed from the
same porcelain evidence (M09-05); postflight is `check_git_state` itself,
called with the last checkpoint the pipeline itself accepted (never the
run's original baseline, which a caller keeps separately for its own
overall-inventory comparison) and `role=None`, so no further delta of any
kind -- including a content-only one -- is authorized once the last role
has stopped running, attempted on every path including a failure, and
never followed by any recovery command. Neither `run.json` nor this module
ever holds full file content, only paths and hashes.

`classify_runtime_root_containment` and `check_runtime_location` implement
this module's other System Design SS6 responsibility, the runtime ignore
probe (M10-01; ADR-008): before any run artifact is created, a candidate
runtime root under a `.git` metadata directory at any depth is always
rejected, and one inside some other Git working tree must already have
itself and a `.probe` sentinel child covered by `git check-ignore
--no-index` -- this module never edits `.gitignore` to make that true. A
runtime root outside any Git working tree entirely needs no ignore check.

`resolve_absolute_git_dir` (M10-02) is a thin, standalone wrapper around
the same allowlisted `--absolute-git-dir` probe `resolve_target` already
uses internally, exposed so `locking.py` can derive a target's lock
coordination directory without repeating the full target preflight.

This module never mutates Git state, never retries beyond the one bounded
resample, and knows nothing about OpenCode.
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
    AgentRole,
    GitCheckRecord,
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


def parse_porcelain_inventory(
    raw: bytes,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split `git status --porcelain=v1 -z` raw bytes into `(staged,
    unstaged, untracked)` display-safe path tuples (M09-05).

    Each record is `XY<space><path>`; a path is staged if `X` is neither
    space nor `?`, tracked-unstaged if `Y` is neither space nor `?`, and
    untracked only when both are `?` -- a path with both a staged and an
    unstaged change (`MM`) lands in both of the first two tuples, exactly
    reflecting its two independent porcelain columns. A staged rename or
    copy (`X` is `R`/`C`) is followed by one extra `-z` record, the
    original path, which this function consumes and does not itself
    report -- content-sensitive rename/copy attribution belongs to the
    `git-state-v1` fingerprint (SS11.2), not this display inventory.
    """

    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    records = iter(split_null_terminated_records(raw))
    for record in records:
        if len(record) < 3:
            continue
        x_char = chr(record[0])
        y_char = chr(record[1])
        path = _display_safe_text(record[3:])

        if x_char in ("R", "C"):
            next(records, None)

        if x_char == "?" and y_char == "?":
            untracked.append(path)
            continue
        if x_char not in (" ", "?"):
            staged.append(path)
        if y_char not in (" ", "?"):
            unstaged.append(path)

    return tuple(staged), tuple(unstaged), tuple(untracked)


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
    staged, unstaged, untracked = parse_porcelain_inventory(first["status"])
    state = GitState(
        root=target.root,
        branch=capture_branch(_display_safe_text(first["branch"])),
        head=capture_head(_display_safe_text(first["head"])),
        porcelain_summary=_display_safe_text(first["status"]),
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
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


# =============================================================================
# M09-04: checkpoint before/after and role mutation policy
# =============================================================================


def check_git_state(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    target: TargetRepository,
    clock: Clock,
    sequence: int,
    purpose: str,
    role: AgentRole | None = None,
    baseline: GitState | None = None,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> GitCheckRecord:
    """Capture a checkpoint for `purpose` and compare it against `baseline`.

    This is the concrete implementation of `GitSafetyPort.check` (System
    Design SS11.3): call it immediately before and after every provider
    attempt -- even one that fails to spawn, times out, or fails protocol
    parsing. The verdict, in order:

    - the underlying capture (`capture_git_state`, M09-03) was itself
      `INDETERMINATE` (an unresolved race, a permission/escape/unsupported-
      type error, or a probe failure) -> `INDETERMINATE`;
    - `baseline` is `None` (nothing to compare against) but the capture is
      incomplete (a detached `HEAD` or an unresolvable commit on what
      should be a fresh baseline) -> `INDETERMINATE`, never guessed `SAFE`;
    - `baseline` is `None` and the capture is complete -> `SAFE`;
    - `branch`/`HEAD` differs from `baseline` -> `UNSAFE`, regardless of
      `role` -- this is a bootstrap-level invariant, not a mutation the
      coder is ever permitted;
    - the fingerprint differs from `baseline` and `role` is not
      `AgentRole.CODER` -> `UNSAFE`;
    - the fingerprint differs and `role` is `AgentRole.CODER` -> `SAFE`,
      the expected and inventoried outcome of a successful edit;
    - otherwise (no drift, no delta) -> `SAFE`.

    CRITICAL for callers: `role`'s tolerance must be scoped to comparing a
    provider attempt's own `after` against that *same* attempt's own
    `before` -- never to a `before` checkpoint compared against the *last
    accepted* checkpoint (System Design SS11.3: "Un delta apparso fra due
    attempt/fasi ... è GIT_SAFETY_ERROR prima dello spawn e non viene
    attribuito al ruolo successivo"). Pass `role=None` for every `before`/
    continuity check, no matter which role is about to run -- only that
    role's own later `after` check, compared against its own `before`, may
    pass its `AgentRole`. Passing the *upcoming* role to a continuity check
    would incorrectly excuse an external mutation that happened between
    phases (during backoff or a control-plane recheck, say) as if it were
    that role's own doing.

    `record.compared_to` is set to `baseline.fingerprint` (or `None` for
    the first checkpoint); pass the result to `target_fingerprint_changed`
    for the separate signal `retry.decide_retry` needs to suppress a
    coder's provider retry.
    """

    capture = capture_git_state(
        process_runner,
        git_executable=git_executable,
        target=target,
        clock=clock,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )

    compared_to = baseline.fingerprint if baseline is not None else None
    safety_status: GitSafetyStatus

    if capture.safety_status is GitSafetyStatus.INDETERMINATE:
        safety_status = GitSafetyStatus.INDETERMINATE
    elif baseline is None:
        incomplete = capture.state.branch is None or capture.state.head is None
        safety_status = (
            GitSafetyStatus.INDETERMINATE if incomplete else GitSafetyStatus.SAFE
        )
    else:
        branch_or_head_drifted = (
            capture.state.branch != baseline.branch
            or capture.state.head != baseline.head
        )
        fingerprint_delta_unsafe = (
            capture.state.fingerprint != baseline.fingerprint
            and role is not AgentRole.CODER
        )
        if branch_or_head_drifted or fingerprint_delta_unsafe:
            safety_status = GitSafetyStatus.UNSAFE
        else:
            safety_status = GitSafetyStatus.SAFE

    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=capture.process_results,
        state=capture.state,
        safety_status=safety_status,
        compared_to=compared_to,
    )


def target_fingerprint_changed(record: GitCheckRecord) -> bool:
    """Return whether `record`'s fingerprint differs from what it was
    compared against (`record.compared_to`, set by `check_git_state` to the
    baseline's fingerprint).

    Always `False` for a checkpoint with nothing to compare against (the
    first-ever checkpoint, `compared_to is None`) and whenever `record`
    is not itself `SAFE` -- an `UNSAFE`/`INDETERMINATE` checkpoint's own
    fingerprint is not a trustworthy content signal, and every case where
    it would matter (an architect/reviewer delta) is already `UNSAFE`
    regardless of this signal. Feed the result to `retry.decide_retry`'s
    `target_changed` parameter, which itself only acts on it for
    `AgentRole.CODER`.
    """

    if record.compared_to is None or record.safety_status is not GitSafetyStatus.SAFE:
        return False
    return record.state.fingerprint != record.compared_to


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


# =============================================================================
# M10-01: runtime root bootstrap -- Git metadata rejection and ignore probe
# =============================================================================

_RUNTIME_IGNORE_SENTINEL_NAME = ".probe"

_CMD_CHECK_IGNORE_PREFIX: tuple[str, ...] = (
    "check-ignore",
    "--quiet",
    "--no-index",
    "--",
)


def classify_runtime_root_containment(runtime_root: Path) -> Path | None:
    """Classify `runtime_root`'s Git containment by pure filesystem walk.

    Raises `PreflightError` immediately and unconditionally if
    `runtime_root` sits under a `.git` metadata directory at any depth --
    this is never softened by ignore status (System Design SS15.1;
    ADR-008). Otherwise returns the nearest containing working tree's
    root: the first ancestor, walking upward and including
    `runtime_root` itself, that has its own `.git` entry -- or `None` if
    `runtime_root` is outside any Git repository entirely, an accepted,
    external location that needs no ignore check.

    This never spawns `git` and never requires `runtime_root` to exist: a
    `.git` entry can be a directory (an ordinary repository) or a file (a
    worktree/submodule pointer) -- either marks its parent as a working
    tree root, but only a directory literally named `.git` counts as
    metadata a path can be "under".
    """

    if not runtime_root.is_absolute():
        raise ValueError("runtime_root must be absolute")

    candidates = (runtime_root, *runtime_root.parents)

    for ancestor in candidates:
        if ancestor.name == ".git" and ancestor.is_dir():
            raise PreflightError(
                "git_safety.runtime_root_under_git_metadata",
                "The runtime root is located under Git metadata.",
                technical_detail=f"runtime_root={runtime_root} git_dir={ancestor}",
            )

    for ancestor in candidates:
        if (ancestor / ".git").exists():
            return ancestor
    return None


def build_check_ignore_argv(
    git_executable: Path, working_tree_root: Path, pathspec: str
) -> tuple[str, ...]:
    """Build `git -C <working_tree_root> check-ignore --quiet --no-index -- <pathspec>`.

    A second, narrower self-verified command shape alongside
    `ALLOWED_GIT_ARGV_TAILS`: unlike every other probe in this module,
    this one must address a caller-supplied candidate path (the runtime
    root or its sentinel), so the variable segment is baked directly into
    the returned tuple rather than checked against a fixed allowlist
    afterwards -- no value of `pathspec` can make this construct a
    different git subcommand (System Design SS15.1; ADR-008). `pathspec`
    is a raw string, not a `Path`, because `check_runtime_root_ignored`
    must sometimes append a trailing `/` that `Path` would silently
    normalize away.
    """

    return (
        str(git_executable),
        "-C",
        str(working_tree_root),
        *_CMD_CHECK_IGNORE_PREFIX,
        pathspec,
    )


def _run_check_ignore_probe(
    process_runner: ProcessRunner,
    git_executable: Path,
    working_tree_root: Path,
    pathspec: str,
    *,
    log_name: str,
    timeout_seconds: float,
    termination_grace_seconds: float,
) -> ProcessResult:
    spec = ProcessSpec(
        argv=build_check_ignore_argv(git_executable, working_tree_root, pathspec),
        cwd=working_tree_root,
        stdin=None,
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    sink = _CapturingSink(path=Path(log_name), max_bytes=_UTILITY_OUTPUT_LIMIT_BYTES)
    return process_runner.run(spec, sink=sink)


def _require_path_ignored(result: ProcessResult, *, code: str, pathspec: str) -> None:
    """Classify one `git check-ignore --quiet --no-index` probe result.

    Exit `0` means ignored (pass). Exit `1` -- a clean, successful run
    that just means "not ignored" -- still leaves `ProcessResult.outcome
    == PROCESS_ERROR`, exactly like any other non-zero exit: `process.py`
    gives `check-ignore`'s two-valued exit code no special meaning, so
    this inspects `return_code` directly rather than trusting `outcome`
    alone. Anything else -- a timeout, an unconfirmed spawn, or any other
    non-zero exit -- fails closed as ambiguous, never guessed ignored
    (NFR-008).
    """

    if result.outcome is RunOutcome.SUCCEEDED:
        return
    if result.outcome is RunOutcome.PROCESS_ERROR and result.return_code == 1:
        raise PreflightError(
            code,
            f"The runtime path is not covered by an existing ignore rule: {pathspec}",
        )
    raise PreflightError(
        code,
        "git check-ignore did not complete successfully.",
        technical_detail=(
            f"outcome={result.outcome.value} return_code={result.return_code}"
        ),
    )


def check_runtime_root_ignored(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    working_tree_root: Path,
    runtime_root: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> None:
    """Require `runtime_root` and a sentinel child to already be
    `git check-ignore`d inside `working_tree_root` (System Design SS15.1;
    ADR-008; FR-015; AC-022). Neither path needs to exist --
    `check-ignore --no-index` is a pure pattern match -- and a miss never
    edits `.gitignore` to fix itself.

    The directory pathspec carries an explicit trailing `/`: `git
    check-ignore` only applies a directory-only (trailing-`/`) `.gitignore`
    pattern -- the natural way to write one -- to a bare pathspec when it
    can otherwise tell the path is a directory, which it cannot for a
    `runtime_root` that does not exist yet (as required by M10-01's "no
    directory is created before validation"). A trailing `/` on the
    pathspec itself makes that determination explicit instead of
    depending on on-disk state, without changing the result for any other
    pattern style. The sentinel is unambiguously a file and is never
    given one.
    """

    sentinel = runtime_root / _RUNTIME_IGNORE_SENTINEL_NAME
    probes = (
        (
            f"{runtime_root}/",
            "git_safety.runtime_root_not_ignored",
            "git-runtime-check-ignore-root.log",
        ),
        (
            str(sentinel),
            "git_safety.runtime_sentinel_not_ignored",
            "git-runtime-check-ignore-sentinel.log",
        ),
    )
    for pathspec, code, log_name in probes:
        result = _run_check_ignore_probe(
            process_runner,
            git_executable,
            working_tree_root,
            pathspec,
            log_name=log_name,
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
        )
        _require_path_ignored(result, code=code, pathspec=pathspec)


def check_runtime_location(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    runtime_root: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> None:
    """Fail closed unless `runtime_root` is safe to hold run artifacts,
    before any of them is created (System Design SS15.1; ADR-008; M10-01).

    A `runtime_root` under Git metadata is always rejected outright. One
    inside some other Git working tree must already have both itself and
    a sentinel child covered by `git check-ignore`; this module never
    edits `.gitignore` to make that true. A `runtime_root` outside any
    Git working tree entirely is accepted without an ignore check
    (FR-015, FR-042; AC-022).
    """

    working_tree_root = classify_runtime_root_containment(runtime_root)
    if working_tree_root is None:
        return
    check_runtime_root_ignored(
        process_runner,
        git_executable=git_executable,
        working_tree_root=working_tree_root,
        runtime_root=runtime_root,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )


# =============================================================================
# M10-02: absolute Git directory resolution for the target-scoped lock
# =============================================================================


def resolve_absolute_git_dir(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    target_root: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> Path:
    """Resolve `target_root`'s absolute Git directory via
    `git rev-parse --absolute-git-dir` (System Design SS16.2; ADR-006),
    independent of the full target preflight in `resolve_target`.

    `locking.py` calls this to derive the coordination directory
    (`<git-dir>/opencode-tools/`) a target-scoped lease and quarantine
    marker live under: two checkouts or worktrees with distinct Git
    directories -- including a linked worktree, whose own `--absolute-
    git-dir` differs from its main checkout's -- get distinct
    coordination directories, so distinct leases (M10-02). A probe
    failure fails closed with `PreflightError`, never guessed.
    """

    result, sink = _run_git_probe(
        process_runner,
        git_executable,
        target_root,
        _CMD_ABSOLUTE_GIT_DIR,
        log_name="git-lock-git-dir.log",
        cwd=target_root,
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    _require_probe_succeeded(
        result,
        code="git_safety.lock_git_dir_probe_failed",
        message="git rev-parse --absolute-git-dir did not complete successfully.",
    )
    return parse_git_common_dir(
        _decode_probe_stdout(sink, code="git_safety.lock_git_dir_probe_failed")
    )


__all__ = (
    "ALLOWED_GIT_ARGV_TAILS",
    "GIT_STATE_FINGERPRINT_VERSION",
    "FingerprintGitlinkEntry",
    "FingerprintPathEntry",
    "GitStateCapture",
    "IndexEntry",
    "build_check_ignore_argv",
    "build_fingerprint_path_entry",
    "build_git_argv",
    "build_git_state_fingerprint",
    "capture_branch",
    "capture_git_state",
    "capture_head",
    "check_branch_attached",
    "check_clean_worktree",
    "check_git_argv_is_allowlisted",
    "check_git_state",
    "check_not_bare",
    "check_runtime_location",
    "check_runtime_root_ignored",
    "check_top_level",
    "classify_lstat_mode",
    "classify_runtime_root_containment",
    "compute_git_state_fingerprint",
    "parse_git_common_dir",
    "parse_index_manifest",
    "parse_porcelain_inventory",
    "resolve_absolute_git_dir",
    "resolve_git_executable",
    "resolve_target",
    "split_null_terminated_records",
    "target_fingerprint_changed",
)

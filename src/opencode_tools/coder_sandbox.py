"""Disposable coder working-tree isolation for Git metadata safety.

The coder must never execute inside the real target repository.  This module
creates an independent local clone, mirrors the target's current uncommitted
working-tree state into it, records that state as a sandbox-only baseline
commit, and later promotes only the coder's delta back into the real target.

The clone uses its own object database (no hard links) and has every remote
removed before OpenCode sees it.  Deleting or reinitializing the sandbox
`.git` therefore cannot destroy the real target's refs, objects, reflog,
index, config, or hooks (GitHub issue #90).

Promotion never runs `git add`.  Staging by path applies whatever clean
filter `.gitattributes` selects for that path -- and Git looks for
`.gitattributes` in *every* directory of the tree, not only the root, with
the filter command itself coming from `.git/config` (local, or the
machine's global/system config). None of that is something the coder-
writable sandbox lets this trusted process enumerate up front. Instead,
`promote_coder_changes` seeds a throwaway index directly from the
sandbox's baseline commit, builds the candidate path set the same way
`git add -A` would -- tracked files plus non-ignored untracked ones, via
`git ls-files --cached --others --exclude-standard`, so ignored artifacts
a coder's own tooling produced are never promoted -- and, for each
candidate, fingerprints its *raw* on-disk bytes in-process (SHA-256 over
mode + content, read in bounded chunks so a single huge candidate can
never be pulled whole into this process's memory; no subprocess per
file). A path whose fingerprint still matches the fingerprint captured
right after the sandbox was created (before the coder ever ran) is left
untouched in the index, so a file only affected by checkout-time
normalization (e.g. an `eol=crlf` `.gitattributes` rule) keeps its exact,
correctly normalized baseline blob rather than being "promoted" back to
its raw checkout form -- and, because most files in a real change set are
untouched, `git hash-object --no-filters` (Git's own documented way to
compute a blob exactly as `add` would while skipping the filter entirely)
only ever runs for the paths actually proven to be new or changed, and
does so by *path* for a regular file (Git streams it directly; this
process never buffers a changed file's content either) -- only a
symlink's small, bounded target is ever passed through this process
itself, via `--stdin`. Only paths proven new or changed, or ones missing
from this run's candidates but present at baseline, update or remove an
index entry, which is then diffed against the baseline commit with
`--no-ext-diff --no-textconv` as well. Every path list involved is
NUL-delimited (`-z` / `--index-info` with `-z`), since a Git filename may
contain a literal newline or tab, and every candidate read/stat/readlink
failure other than "does not exist" (including a directory replaced by a
file, or vice versa) fails closed as `CoderSandboxError`. No attribute or
filter lookup is ever consulted for the promoted content. A baseline
gitlink (mode `160000`, a submodule reference) is a tree/index concept
with no regular working-tree representation of its own -- `prepare_coder_sandbox`
clones with `--no-recurse-submodules`, so its path is an empty directory
or entirely absent -- and is therefore never run through the file
classifier at all: it is preserved exactly as `read-tree` already seeded
it, and promotion fails closed instead if the sandbox's own index ever
disagrees with the baseline about a gitlink's pointer (this PR does not
support promoting an intentional submodule pointer change). The earlier
`.gitattributes`/`.git/config`/`.git/info/attributes` byte-identity checks
against the sandbox's pristine baseline remain as defense in depth
(GitHub issue #110).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

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
_MAX_INTEGRITY_FILE_BYTES = 1024 * 1024
_FINGERPRINT_CHUNK_BYTES = 1024 * 1024
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
    gitattributes_baseline: bytes | None
    git_config_baseline: bytes
    git_info_attributes_baseline: bytes | None
    pristine_raw_snapshot: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.container_root.is_absolute():
            raise ValueError("container_root must be absolute")
        if not self.root.is_absolute():
            raise ValueError("root must be absolute")
        if not self.baseline_head:
            raise ValueError("baseline_head must not be empty")
        if not isinstance(self.target_state, GitState):
            raise TypeError("target_state must be GitState")
        if self.gitattributes_baseline is not None and not isinstance(
            self.gitattributes_baseline, bytes
        ):
            raise TypeError("gitattributes_baseline must be bytes or None")
        if not isinstance(self.git_config_baseline, bytes):
            raise TypeError("git_config_baseline must be bytes")
        if self.git_info_attributes_baseline is not None and not isinstance(
            self.git_info_attributes_baseline, bytes
        ):
            raise TypeError("git_info_attributes_baseline must be bytes or None")
        if not isinstance(self.pristine_raw_snapshot, Mapping):
            raise TypeError("pristine_raw_snapshot must be a Mapping")


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


def _read_integrity_file_or_none(path: Path) -> bytes | None:
    """Read one of the sandbox's Git integrity-sensitive files.

    Used for `.gitattributes`, `.git/config`, and `.git/info/attributes`,
    each of which is coder-writable and must be compared byte-for-byte
    against its pristine baseline before `git add`/`git diff` ever touch the
    sandbox (GH #110). Fails closed rather than reading through a symlink,
    accepting a non-regular file (a FIFO would hang a plain read), or
    reading past a bound -- any of those is itself a sign of tampering, not
    a legitimate state, given a plain `git clone` never produces one.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CoderSandboxError(
            f"{path} could not be safely read as a regular, non-symlinked file; "
            "sandbox changes were not promoted."
        ) from error
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise CoderSandboxError(
                f"{path} is unexpectedly not a regular file; "
                "sandbox changes were not promoted."
            )
        data = os.read(fd, _MAX_INTEGRITY_FILE_BYTES + 1)
        if len(data) > _MAX_INTEGRITY_FILE_BYTES:
            raise CoderSandboxError(
                f"{path} unexpectedly exceeds the bounded read size; "
                "sandbox changes were not promoted."
            )
        return data
    finally:
        os.close(fd)


def _classify_and_fingerprint(entry_path: Path) -> tuple[str, str] | None:
    """Return (mode, fingerprint) for `entry_path`, or `None` if it is
    absent or has been replaced by a directory (a previously-tracked file
    path becoming a directory is not itself a promotable entry -- any new
    content underneath is separately enumerated).

    A regular file is never read into memory whole: it is fingerprinted
    directly from disk in bounded chunks, so a single very large candidate
    -- already present at baseline, or newly created by the coder -- can
    never be pulled entirely into this process's memory (GH #110
    follow-up). A symlink's target is always small (bounded by the
    platform's `PATH_MAX`) and is read whole, matching how a symlink is
    represented as a Git blob.

    `NotADirectoryError` is treated the same as "does not exist": it means
    an ancestor of this path -- a directory at baseline -- was replaced by
    a regular file, so every one of its former children is, from this
    path's perspective, simply gone (a valid directory-to-file transition,
    not a failure).

    Every other failure -- a coder-unreadable file via `chmod 000`, an
    unsupported file type such as a FIFO -- fails closed as
    `CoderSandboxError` rather than escaping as a raw `OSError` the caller
    only catches as `CoderSandboxError`.
    """
    try:
        entry_lstat = os.lstat(entry_path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        raise CoderSandboxError(
            f"{entry_path} could not be inspected; sandbox changes were not promoted."
        ) from error

    if stat.S_ISDIR(entry_lstat.st_mode):
        return None
    if stat.S_ISLNK(entry_lstat.st_mode):
        try:
            target = os.readlink(os.fsencode(entry_path))
        except OSError as error:
            raise CoderSandboxError(
                f"{entry_path} could not be read; sandbox changes were not promoted."
            ) from error
        return "120000", _fingerprint_bytes("120000", target)
    if not stat.S_ISREG(entry_lstat.st_mode):
        raise CoderSandboxError(
            f"{entry_path} is an unsupported file type; "
            "sandbox changes were not promoted."
        )

    mode = "100755" if entry_lstat.st_mode & 0o111 else "100644"
    try:
        return mode, _fingerprint_file_chunked(entry_path, mode)
    except OSError as error:
        raise CoderSandboxError(
            f"{entry_path} could not be read; sandbox changes were not promoted."
        ) from error


def _fingerprint_bytes(mode: str, content: bytes) -> str:
    """A cheap, in-process, collision-resistant fingerprint of (mode,
    content), used purely for pristine-vs-current equality comparison --
    never as a Git object id. Only used for a symlink's target, which is
    always small; a regular file is fingerprinted by `_fingerprint_file_chunked`
    instead, without ever holding its full content in memory."""
    digest = hashlib.sha256()
    digest.update(mode.encode("ascii"))
    digest.update(b"\x00")
    digest.update(content)
    return digest.hexdigest()


def _fingerprint_file_chunked(entry_path: Path, mode: str) -> str:
    """Fingerprint a regular file's content by reading it in bounded
    chunks, so peak memory use is independent of file size (GH #110
    follow-up: a multi-gigabyte tracked or coder-created file must not be
    able to exhaust this process's memory)."""
    digest = hashlib.sha256()
    digest.update(mode.encode("ascii"))
    digest.update(b"\x00")
    with entry_path.open("rb") as handle:
        while chunk := handle.read(_FINGERPRINT_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_stdin_blob(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    cwd: Path,
    content: bytes,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> str:
    """Hash small `content` (a symlink target) exactly as `git add` would,
    minus the clean filter. Never used for a regular file's content --
    see `_hash_path_blob`, which lets Git stream the file itself."""
    _, sha_raw = _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=cwd,
        argv_tail=(
            "-C",
            str(cwd),
            "hash-object",
            "--no-filters",
            "-w",
            "-t",
            "blob",
            "--stdin",
        ),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        stdin=content,
        log_name="coder-sandbox-hash-object-stdin.log",
    )
    return _single_line(sha_raw, field_name="blob hash")


def _hash_path_blob(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    cwd: Path,
    entry_path: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> str:
    """Hash a regular file exactly as `git add` would, minus the clean
    filter, by giving Git the path directly. Git reads/streams the file
    itself, so this process never buffers a changed file's content --
    empirically confirmed `--no-filters` bypasses the clean filter the
    same way whether content arrives by path or by `--stdin` (GH #110
    follow-up)."""
    _, sha_raw = _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=cwd,
        argv_tail=(
            "-C",
            str(cwd),
            "hash-object",
            "--no-filters",
            "-w",
            "-t",
            "blob",
            "--",
            str(entry_path),
        ),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        log_name="coder-sandbox-hash-object-path.log",
    )
    return _single_line(sha_raw, field_name="blob hash")


def _capture_raw_snapshot(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    sandbox_root: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> Mapping[str, str]:
    """Fingerprint every tracked path's raw on-disk (checkout) bytes right
    after the sandbox's baseline commit, before the coder ever runs.

    Used at promotion time to tell an actual coder edit apart from mere
    checkout-time normalization (e.g. an `eol=crlf` `.gitattributes` rule)
    on a file the coder never touched (GH #110 follow-up). Everything is
    tracked at this point (the baseline commit was just created), so
    `--cached` alone is the full candidate set. Fingerprinting is done
    entirely in-process (no `git hash-object` per file) -- only the single
    `ls-files` call below spawns a subprocess.
    """
    _, candidates_raw = _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=sandbox_root,
        argv_tail=("-C", str(sandbox_root), "ls-files", "-z", "--cached"),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        log_name="coder-sandbox-baseline-ls-files.log",
    )
    snapshot: dict[str, str] = {}
    for entry in candidates_raw.split(b"\x00"):
        if not entry:
            continue
        rel_path = os.fsdecode(entry)
        classified = _classify_and_fingerprint(sandbox_root / rel_path)
        if classified is None:
            continue
        _mode, fingerprint = classified
        snapshot[rel_path] = fingerprint
    return MappingProxyType(snapshot)


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

        git_config_baseline = _read_integrity_file_or_none(git_dir / "config")
        if git_config_baseline is None:
            raise CoderSandboxError(
                "The coder sandbox's .git/config is missing right after creation."
            )

        pristine_raw_snapshot = _capture_raw_snapshot(
            process_runner,
            git_executable=git_executable,
            sandbox_root=sandbox_root,
            utility_timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
        )

        return CoderSandbox(
            container_root=container_root,
            root=sandbox_root,
            baseline_head=baseline_head,
            target_state=capture.state,
            gitattributes_baseline=_read_integrity_file_or_none(
                sandbox_root / ".gitattributes"
            ),
            git_config_baseline=git_config_baseline,
            git_info_attributes_baseline=_read_integrity_file_or_none(
                git_dir / "info" / "attributes"
            ),
            pristine_raw_snapshot=pristine_raw_snapshot,
        )
    except Exception:
        shutil.rmtree(container_root, ignore_errors=True)
        raise


_GITLINK_MODE = "160000"


def _parse_ls_files_stage(raw: bytes) -> dict[str, tuple[str, str]]:
    """Parse NUL-delimited `git ls-files -z --stage` output into
    `{path: (mode, sha)}`. A merge-conflicted entry (stage != 0) is
    skipped rather than misparsed -- `promote_coder_changes` already
    requires a clean, single-commit sandbox HEAD, so none are expected
    here."""
    entries: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\x00"):
        if not record:
            continue
        metadata, _tab, path_bytes = record.partition(b"\t")
        mode_bytes, _sp, rest = metadata.partition(b" ")
        sha_bytes, _sp, stage_bytes = rest.partition(b" ")
        if stage_bytes != b"0":
            continue
        entries[os.fsdecode(path_bytes)] = (
            mode_bytes.decode("ascii"),
            sha_bytes.decode("ascii"),
        )
    return entries


def _build_promotion_patch(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    sandbox: CoderSandbox,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> bytes:
    """Build the promotion patch without ever staging through `git add`.

    The throwaway index is seeded directly from the sandbox's baseline
    commit, so every path keeps its exact, correctly checkout-normalized
    baseline blob unless it is proven to have actually changed. The
    candidate path set is exactly what `git add -A` would have considered
    -- tracked files plus non-ignored untracked ones, via `git ls-files
    --cached --others --exclude-standard` -- so ignored artifacts a
    coder's own tooling produced (build output, dependency installs,
    caches) are never promoted. Each candidate's *raw* on-disk bytes are
    fingerprinted in-process (no subprocess) and compared against the
    fingerprint captured for that same path right after the sandbox was
    created: a match means nothing actually changed (e.g. an `eol=crlf`
    `.gitattributes` rule alone does not count), so the baseline entry
    already seeded into the index is left alone with no further work; only
    a real mismatch or a brand-new path spawns `git hash-object
    --no-filters` (never through the clean-filter/attribute machinery,
    and by path for a regular file so Git streams it rather than this
    process buffering it) to get an actual blob id. A path missing from
    this run's candidates but
    present at baseline is removed. Removals are applied with
    `--force-remove` and *before* additions, so a directory-to-file (or
    file-to-directory) transition never leaves the index in a conflicting
    state where both the old and new entry are present at once. Every path
    list here is NUL-delimited (`-z`), since Git filenames may contain a
    literal newline, tab, or any other byte but NUL.
    """
    promote_index = sandbox.container_root / "promote.index"
    index_env = {"GIT_INDEX_FILE": str(promote_index)}

    _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=sandbox.root,
        argv_tail=("-C", str(sandbox.root), "read-tree", sandbox.baseline_head),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        environment_overrides=index_env,
        log_name="coder-sandbox-promote-read-tree-baseline.log",
    )

    # A gitlink (mode 160000, a submodule reference) has no regular
    # working-tree representation of its own to fingerprint -- the clone
    # in prepare_coder_sandbox uses --no-recurse-submodules, so its path
    # is an empty directory or entirely absent -- so it must never be run
    # through the file classifier below (GH #110 follow-up: that
    # misclassified it as deleted on every promotion, including a no-op
    # one). Read it back from the index we just seeded, and preserve it
    # exactly as-is by simply never touching that path again here. The
    # sandbox's own real index is checked against the same baseline as a
    # fail-closed guard: this PR does not support promoting an
    # intentional submodule pointer change, so if one was somehow
    # attempted, refuse the whole promotion rather than silently
    # dropping or mishandling it.
    _, baseline_stage_raw = _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=sandbox.root,
        argv_tail=("-C", str(sandbox.root), "ls-files", "-z", "--stage"),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        environment_overrides=index_env,
        log_name="coder-sandbox-promote-baseline-stage.log",
    )
    baseline_gitlinks = {
        path: sha
        for path, (mode, sha) in _parse_ls_files_stage(baseline_stage_raw).items()
        if mode == _GITLINK_MODE
    }
    if baseline_gitlinks:
        _, sandbox_stage_raw = _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=sandbox.root,
            argv_tail=("-C", str(sandbox.root), "ls-files", "-z", "--stage"),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            log_name="coder-sandbox-promote-sandbox-stage.log",
        )
        sandbox_gitlinks = {
            path: sha
            for path, (mode, sha) in _parse_ls_files_stage(sandbox_stage_raw).items()
            if mode == _GITLINK_MODE
        }
        if sandbox_gitlinks != baseline_gitlinks:
            raise CoderSandboxError(
                "The coder changed a submodule reference; sandbox changes were not promoted."
            )

    _, candidates_raw = _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=sandbox.root,
        argv_tail=(
            "-C",
            str(sandbox.root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        environment_overrides=index_env,
        log_name="coder-sandbox-promote-ls-files.log",
    )
    current_paths = sorted(
        {os.fsdecode(entry) for entry in candidates_raw.split(b"\x00") if entry}
    )

    changed_entries: list[bytes] = []
    removed_entries: list[bytes] = []
    seen_paths: set[str] = set()

    for rel_path in current_paths:
        seen_paths.add(rel_path)
        if rel_path in baseline_gitlinks:
            continue  # preserved exactly as read-tree already seeded it
        entry_path = sandbox.root / rel_path
        classified = _classify_and_fingerprint(entry_path)
        if classified is None:
            removed_entries.append(os.fsencode(rel_path) + b"\x00")
            continue
        mode, fingerprint = classified
        if sandbox.pristine_raw_snapshot.get(rel_path) == fingerprint:
            continue  # untouched since the sandbox was created; no subprocess needed

        if mode == "120000":
            try:
                target = os.readlink(os.fsencode(entry_path))
            except OSError as error:
                raise CoderSandboxError(
                    f"{entry_path} could not be read; sandbox changes were not promoted."
                ) from error
            sha = _hash_stdin_blob(
                process_runner,
                git_executable=git_executable,
                cwd=sandbox.root,
                content=target,
                utility_timeout_seconds=utility_timeout_seconds,
                termination_grace_seconds=termination_grace_seconds,
            )
        else:
            sha = _hash_path_blob(
                process_runner,
                git_executable=git_executable,
                cwd=sandbox.root,
                entry_path=entry_path,
                utility_timeout_seconds=utility_timeout_seconds,
                termination_grace_seconds=termination_grace_seconds,
            )
        changed_entries.append(
            f"{mode} {sha} 0\t".encode() + os.fsencode(rel_path) + b"\x00"
        )

    for rel_path in sandbox.pristine_raw_snapshot:
        if rel_path not in seen_paths:
            removed_entries.append(os.fsencode(rel_path) + b"\x00")

    # Removals must land before additions: a directory<->file transition
    # means the candidate set contains both the old and the new shape of
    # the same path, and applying the addition first leaves the old entry
    # in place, turning the removal into a conflicting no-op (GH #110
    # follow-up).
    if removed_entries:
        _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=sandbox.root,
            argv_tail=(
                "-C",
                str(sandbox.root),
                "update-index",
                "-z",
                "--force-remove",
                "--stdin",
            ),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            stdin=b"".join(removed_entries),
            environment_overrides=index_env,
            log_name="coder-sandbox-promote-update-index-remove.log",
        )
    if changed_entries:
        _run_git(
            process_runner,
            git_executable=git_executable,
            cwd=sandbox.root,
            argv_tail=(
                "-C",
                str(sandbox.root),
                "update-index",
                "-z",
                "--add",
                "--index-info",
            ),
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            stdin=b"".join(changed_entries),
            environment_overrides=index_env,
            log_name="coder-sandbox-promote-update-index.log",
        )

    _, patch = _run_git(
        process_runner,
        git_executable=git_executable,
        cwd=sandbox.root,
        argv_tail=(
            "-C",
            str(sandbox.root),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--cached",
            "--binary",
            "--full-index",
            sandbox.baseline_head,
            "--",
        ),
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        environment_overrides=index_env,
        log_name="coder-sandbox-promote-diff.log",
    )
    return patch


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

    # Defense in depth (GH #110): a clean filter or textconv/external-diff
    # driver declared anywhere the coder can reach -- root or nested
    # .gitattributes, .git/config -- would run as this trusted orchestrator
    # process, not the coder's own sandbox. The patch built below never
    # stages by path, so none of this is actually consulted for content;
    # still refuse promotion if the sandbox's own integrity files were
    # touched, since that is itself a sign of tampering worth failing on.
    if (
        _read_integrity_file_or_none(sandbox.root / ".gitattributes")
        != sandbox.gitattributes_baseline
    ):
        raise CoderSandboxError(
            "The coder changed .gitattributes; sandbox changes were not promoted."
        )
    current_git_config = _read_integrity_file_or_none(git_dir / "config")
    if current_git_config is None or current_git_config != sandbox.git_config_baseline:
        raise CoderSandboxError(
            "The coder changed the sandbox's local Git configuration; sandbox changes were not promoted."
        )
    if (
        _read_integrity_file_or_none(git_dir / "info" / "attributes")
        != sandbox.git_info_attributes_baseline
    ):
        raise CoderSandboxError(
            "The coder added Git attribute overrides outside version control; sandbox changes were not promoted."
        )

    patch = _build_promotion_patch(
        process_runner,
        git_executable=git_executable,
        sandbox=sandbox,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
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

"""Target-scoped, non-blocking POSIX lock lease (System Design SS16; ADR-006;
ADR-009; M10-02).

Two conforming runs on the same checkout would invalidate baseline
attribution and review; a lock inside the configurable runtime location
could be bypassed by pointing `runtime.root` elsewhere. This module instead
derives its coordination directory from the target's own absolute Git
directory (`git_safety.resolve_absolute_git_dir`) -- `<git-dir>/opencode-
tools/` -- so distinct `runtime.root` values never change the lock's key,
and two checkouts or linked worktrees with distinct Git directories always
get distinct leases and can proceed in parallel.

`PosixTargetLeaseFactory.acquire` (`ports.TargetLeaseFactory`) opens (or
creates, mode `0600`) `target.lock` under that directory and attempts
`fcntl.flock(LOCK_EX | LOCK_NB)`. A lease already held by another process
fails closed with `PreflightError` (`locking.target_locked`), including
whatever diagnostic holder metadata is readable, before any agent runs; a
filesystem without reliable advisory locking fails closed distinctly
(`locking.advisory_lock_unavailable`). Diagnostic holder metadata -- run
ID, PID, host, timestamp, a target digest, and the configured runtime root
-- is written only after acquisition, is purely informational, and is
never used to guess a lock stale by PID or age: presence of `target.lock`
on disk never implies an active lock, and a residual file with no live
`flock` never blocks a fresh acquisition.

The returned `PosixTargetLease` (`ports.TargetLease`) releases the OS lock
on `__exit__`. Quarantine for an unconfirmed process-group termination is
M10-03's addition to this same module.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import stat
from pathlib import Path
from typing import Self

from opencode_tools.errors import PreflightError
from opencode_tools.git_safety import resolve_absolute_git_dir
from opencode_tools.ports import Clock, ProcessRunner

COORDINATION_DIRECTORY_NAME = "opencode-tools"
LOCK_FILENAME = "target.lock"
LOCK_METADATA_FILENAME = "target.lock.meta.json"

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


def coordination_directory(git_common_dir: Path) -> Path:
    """Return `<git_common_dir>/opencode-tools`, never under the working
    tree and identical for every run against the same physical checkout
    regardless of `runtime.root` (System Design SS16.2; ADR-006)."""

    return git_common_dir / COORDINATION_DIRECTORY_NAME


def target_lock_path(git_common_dir: Path) -> Path:
    return coordination_directory(git_common_dir) / LOCK_FILENAME


def _target_digest(target_root: Path) -> str:
    return hashlib.sha256(str(target_root).encode("utf-8")).hexdigest()


def _format_timestamp(clock: Clock) -> str:
    return clock.now().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _ensure_coordination_directory(path: Path) -> None:
    """Create `path` (mode `0700`) if missing, or verify it if it already
    exists: a real, non-symlink directory with no group/other bits.
    Mirrors `runlog.py`'s own runtime-root verification (M10-01) but is
    kept local to this module, which has no other reason to depend on
    `runlog.py`.
    """

    try:
        os.mkdir(path, _DIRECTORY_MODE)
    except FileExistsError:
        pass
    except OSError as error:
        raise PreflightError(
            "locking.coordination_directory_create_failed",
            f"failed to create the lock coordination directory: {path}",
            technical_detail=type(error).__name__,
        ) from None

    try:
        info = os.lstat(path)
    except OSError as error:
        raise PreflightError(
            "locking.coordination_directory_unverifiable",
            f"could not verify the lock coordination directory: {path}",
            technical_detail=type(error).__name__,
        ) from None
    if stat.S_ISLNK(info.st_mode):
        raise PreflightError(
            "locking.coordination_directory_is_symlink",
            f"refusing a symlinked lock coordination directory: {path}",
        )
    if not stat.S_ISDIR(info.st_mode):
        raise PreflightError(
            "locking.coordination_directory_not_a_directory",
            f"expected a directory but found something else: {path}",
        )
    if stat.S_IMODE(info.st_mode) & ~_DIRECTORY_MODE:
        raise PreflightError(
            "locking.coordination_directory_mode_too_permissive",
            f"lock coordination directory mode exceeds {oct(_DIRECTORY_MODE)}: {path}",
        )


def _write_holder_metadata(
    path: Path,
    *,
    run_id: str,
    target_root: Path,
    runtime_root: Path,
    clock: Clock,
) -> None:
    """Best-effort record of who now holds the lease (System Design
    SS16.2). Purely diagnostic: a write failure here never invalidates an
    already-acquired `flock`, so it is swallowed rather than raised.
    """

    payload = json.dumps(
        {
            "run_id": run_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": _format_timestamp(clock),
            "target_digest": _target_digest(target_root),
            "runtime_root": str(runtime_root),
        }
    ).encode("utf-8")
    try:
        file_descriptor = os.open(
            path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _FILE_MODE
        )
        try:
            os.write(file_descriptor, payload)
        finally:
            os.close(file_descriptor)
    except OSError:
        pass


def _read_holder_metadata(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="backslashreplace")
    except OSError:
        return None


class PosixTargetLease:
    """A held, non-blocking `flock` lease on one target's coordination
    lock file (`ports.TargetLease`; ADR-006; M10-02)."""

    def __init__(
        self,
        file_descriptor: int,
        *,
        lock_path: Path,
        coordination_dir: Path,
        run_id: str,
        target_root: Path,
        clock: Clock,
    ) -> None:
        self._file_descriptor = file_descriptor
        self._lock_path = lock_path
        self._coordination_dir = coordination_dir
        self._run_id = run_id
        self._target_root = target_root
        self._clock = clock
        self._released = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self.release()

    def release(self) -> None:
        """Release the OS lock. Idempotent; the lock file itself is left
        on disk -- its presence never means an active lock (ADR-006)."""

        if self._released:
            return
        try:
            os.close(self._file_descriptor)
        finally:
            self._released = True


class PosixTargetLeaseFactory:
    """Concrete `ports.TargetLeaseFactory`: a target-scoped, non-blocking
    POSIX `flock` lease under the target's own Git directory (System
    Design SS16.2; ADR-006; M10-02)."""

    def __init__(
        self,
        process_runner: ProcessRunner,
        *,
        git_executable: Path,
        clock: Clock,
        utility_timeout_seconds: float,
        termination_grace_seconds: float,
    ) -> None:
        self._process_runner = process_runner
        self._git_executable = git_executable
        self._clock = clock
        self._utility_timeout_seconds = utility_timeout_seconds
        self._termination_grace_seconds = termination_grace_seconds

    def acquire(
        self,
        target_root: Path,
        runtime_root: Path,
        run_id: str,
    ) -> PosixTargetLease:
        git_common_dir = resolve_absolute_git_dir(
            self._process_runner,
            git_executable=self._git_executable,
            target_root=target_root,
            utility_timeout_seconds=self._utility_timeout_seconds,
            termination_grace_seconds=self._termination_grace_seconds,
        )
        coordination_dir = coordination_directory(git_common_dir)
        _ensure_coordination_directory(coordination_dir)
        lock_path = coordination_dir / LOCK_FILENAME

        file_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, _FILE_MODE)
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _read_holder_metadata(coordination_dir / LOCK_METADATA_FILENAME)
            os.close(file_descriptor)
            raise PreflightError(
                "locking.target_locked",
                "The target is already locked by another run.",
                technical_detail=holder,
            ) from None
        except OSError as error:
            os.close(file_descriptor)
            raise PreflightError(
                "locking.advisory_lock_unavailable",
                "This filesystem does not support a reliable advisory lock.",
                technical_detail=type(error).__name__,
            ) from None

        _write_holder_metadata(
            coordination_dir / LOCK_METADATA_FILENAME,
            run_id=run_id,
            target_root=target_root,
            runtime_root=runtime_root,
            clock=self._clock,
        )
        return PosixTargetLease(
            file_descriptor,
            lock_path=lock_path,
            coordination_dir=coordination_dir,
            run_id=run_id,
            target_root=target_root,
            clock=self._clock,
        )


__all__ = (
    "COORDINATION_DIRECTORY_NAME",
    "LOCK_FILENAME",
    "LOCK_METADATA_FILENAME",
    "PosixTargetLease",
    "PosixTargetLeaseFactory",
    "coordination_directory",
    "target_lock_path",
)

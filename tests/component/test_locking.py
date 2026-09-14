"""Component tests for the M10-02 target-scoped, non-blocking POSIX lock
lease and the M10-03 persistent quarantine (System Design SS16; ADR-006;
ADR-009).

Builds real temporary Git repositories (including linked worktrees) and
drives `PosixTargetLeaseFactory`/`PosixTargetLease` through the real
`SubprocessRunner` and a real `git` executable. A genuine second OS process
(`tests/component/helpers/lock_holder.py`) proves real cross-process kernel
contention and release, not just same-process `flock` semantics.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools import locking as locking_module
from opencode_tools.errors import LoggingError, PreflightError
from opencode_tools.git_safety import resolve_absolute_git_dir, resolve_git_executable
from opencode_tools.locking import (
    LOCK_METADATA_FILENAME,
    PosixTargetLeaseFactory,
    coordination_directory,
    quarantine_path,
    target_lock_path,
)
from opencode_tools.process import SubprocessRunner

UTILITY_TIMEOUT_SECONDS = 5.0
TERMINATION_GRACE_SECONDS = 1.0

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

GIT_EXECUTABLE = resolve_git_executable()
LOCK_HOLDER_HELPER = Path(__file__).resolve().parent / "helpers" / "lock_holder.py"


class RealClock:
    """A `Clock` reading genuine wall/monotonic time for a real subprocess."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True
    )


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / "file.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)
    return root


def _factory() -> PosixTargetLeaseFactory:
    return PosixTargetLeaseFactory(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        clock=RealClock(),
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


def _git_dir(target_root: Path) -> Path:
    return resolve_absolute_git_dir(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        target_root=target_root,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


# --- basic lifecycle ---------------------------------------------------------


def test_acquire_creates_a_private_coordination_directory_and_lock_file(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")

    with _factory().acquire(repo, tmp_path / ".opencode-tools", "run-001") as lease:
        del lease
        git_dir = _git_dir(repo)
        coordination_dir = coordination_directory(git_dir)
        lock_path = target_lock_path(git_dir)

        assert coordination_dir.is_dir()
        assert stat.S_IMODE(coordination_dir.stat().st_mode) == 0o700
        assert lock_path.is_file()
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

        metadata_path = coordination_dir / LOCK_METADATA_FILENAME
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["run_id"] == "run-001"
        assert metadata["pid"] == os.getpid()
        assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600


def test_release_is_idempotent_and_leaves_the_lock_file_on_disk(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    lease = _factory().acquire(repo, tmp_path / ".opencode-tools", "run-001")

    lease.release()
    lease.release()

    assert target_lock_path(_git_dir(repo)).exists()


# --- AC: same target contention ----------------------------------------------


def test_acquire_rejects_a_second_lease_on_the_same_target(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"
    holder = _factory().acquire(repo, runtime_root, "run-001")

    with pytest.raises(PreflightError) as exc_info:
        _factory().acquire(repo, runtime_root, "run-002")

    assert exc_info.value.code == "locking.target_locked"
    assert exc_info.value.technical_detail is not None
    holder_metadata = json.loads(exc_info.value.technical_detail)
    assert holder_metadata["run_id"] == "run-001"

    holder.release()


def test_acquire_succeeds_once_the_holder_releases(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"
    holder = _factory().acquire(repo, runtime_root, "run-001")
    holder.release()

    with _factory().acquire(repo, runtime_root, "run-002") as lease:
        del lease


# --- AC: different targets proceed in parallel -------------------------------


def test_acquire_allows_different_targets_to_proceed_in_parallel(
    tmp_path: Path,
) -> None:
    repo_a = _init_repo(tmp_path / "repo-a")
    repo_b = _init_repo(tmp_path / "repo-b")
    runtime_root = tmp_path / ".opencode-tools"

    with (
        _factory().acquire(repo_a, runtime_root, "run-001") as lease_a,
        _factory().acquire(repo_b, runtime_root, "run-002") as lease_b,
    ):
        del lease_a, lease_b


# --- AC: runtime root does not change the lock's key -------------------------


def test_acquire_key_is_independent_of_runtime_root(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    holder = _factory().acquire(repo, tmp_path / "runtime-a", "run-001")

    with pytest.raises(PreflightError) as exc_info:
        _factory().acquire(repo, tmp_path / "runtime-b", "run-002")
    assert exc_info.value.code == "locking.target_locked"

    holder.release()


# --- distinct worktrees get distinct leases ----------------------------------


def test_acquire_gives_a_linked_worktree_its_own_lease(tmp_path: Path) -> None:
    main_repo = _init_repo(tmp_path / "main")
    worktree = tmp_path / "worktree"
    _git(
        ["worktree", "add", "-q", str(worktree), "-b", "wt-branch"],
        cwd=main_repo,
    )
    runtime_root = tmp_path / ".opencode-tools"

    assert _git_dir(main_repo) != _git_dir(worktree)

    with (
        _factory().acquire(main_repo, runtime_root, "run-001") as lease_main,
        _factory().acquire(worktree, runtime_root, "run-002") as lease_worktree,
    ):
        del lease_main, lease_worktree


# --- residual lock file is harmless ------------------------------------------


def test_a_residual_lock_file_without_an_active_flock_does_not_block(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"

    first = _factory().acquire(repo, runtime_root, "run-001")
    first.release()
    lock_path = target_lock_path(_git_dir(repo))
    assert lock_path.exists()

    with _factory().acquire(repo, runtime_root, "run-002") as second:
        del second


# --- genuine cross-process contention and kernel release --------------------


def _wait_for_file(path: Path, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"{path} did not appear in time")


def test_acquire_is_rejected_by_a_genuinely_separate_holding_process(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"
    lock_path = target_lock_path(_git_dir(repo))
    # Bootstrap the coordination directory/lock file with the same mode
    # `acquire()` itself would create them with, then release immediately
    # so the OS lock is free for the real holder process below.
    _factory().acquire(repo, runtime_root, "run-000").release()
    ready_marker = tmp_path / "holder-ready"

    holder_process = subprocess.Popen(
        [sys.executable, str(LOCK_HOLDER_HELPER), str(lock_path), str(ready_marker)]
    )
    try:
        _wait_for_file(ready_marker)

        with pytest.raises(PreflightError) as exc_info:
            _factory().acquire(repo, runtime_root, "run-002")
        assert exc_info.value.code == "locking.target_locked"
    finally:
        holder_process.kill()
        holder_process.wait(timeout=5)


def test_acquire_succeeds_after_the_holder_process_is_killed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"
    lock_path = target_lock_path(_git_dir(repo))
    # Bootstrap the coordination directory/lock file with the same mode
    # `acquire()` itself would create them with, then release immediately
    # so the OS lock is free for the real holder process below.
    _factory().acquire(repo, runtime_root, "run-000").release()
    ready_marker = tmp_path / "holder-ready"

    holder_process = subprocess.Popen(
        [sys.executable, str(LOCK_HOLDER_HELPER), str(lock_path), str(ready_marker)]
    )
    _wait_for_file(ready_marker)
    holder_process.kill()
    holder_process.wait(timeout=5)

    # The kernel releases the flock when the killed process's last file
    # descriptor referencing it closes -- no PID/time staleness heuristic
    # is involved, and the residual lock file itself is harmless.
    with _factory().acquire(repo, runtime_root, "run-002") as lease:
        del lease


# --- AC: an unreliable advisory lock fails closed ----------------------------


def test_acquire_fails_closed_when_flock_is_unreliable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"

    def _raise(file_descriptor: int, operation: int) -> None:
        raise OSError("simulated filesystem without reliable advisory locking")

    monkeypatch.setattr(fcntl, "flock", _raise)

    with pytest.raises(PreflightError) as exc_info:
        _factory().acquire(repo, runtime_root, "run-001")
    assert exc_info.value.code == "locking.advisory_lock_unavailable"


# =============================================================================
# M10-03: persistent quarantine for an unconfirmed termination
# =============================================================================


def test_quarantine_writes_an_atomic_mode_0600_marker(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"
    lease = _factory().acquire(repo, runtime_root, "run-001")

    lease.quarantine("unconfirmed process-group termination")

    marker_path = quarantine_path(_git_dir(repo))
    assert marker_path.is_file()
    assert stat.S_IMODE(marker_path.stat().st_mode) == 0o600
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["run_id"] == "run-001"
    assert marker["reason"] == "unconfirmed process-group termination"
    assert marker["pid"] == os.getpid()

    lease.release()


def test_quarantine_blocks_a_new_run_even_without_an_active_lock(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"
    lease = _factory().acquire(repo, runtime_root, "run-001")
    lease.quarantine("unconfirmed process-group termination")
    lease.release()

    with pytest.raises(PreflightError) as exc_info:
        _factory().acquire(repo, runtime_root, "run-002")
    assert exc_info.value.code == "locking.target_quarantined"


def test_quarantine_marker_is_never_removed_automatically(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"
    lease = _factory().acquire(repo, runtime_root, "run-001")
    lease.quarantine("unconfirmed process-group termination")
    lease.release()
    marker_path = quarantine_path(_git_dir(repo))

    for attempt in range(3):
        with pytest.raises(PreflightError):
            _factory().acquire(repo, runtime_root, f"run-{attempt + 2:03d}")

    assert marker_path.exists()


def test_quarantine_write_failure_raises_logging_error_and_preserves_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = tmp_path / ".opencode-tools"
    lease = _factory().acquire(repo, runtime_root, "run-001")

    def _raise(path: Path, payload: bytes) -> None:
        raise OSError("simulated disk-full quarantine write failure")

    monkeypatch.setattr(locking_module, "write_private_file_atomically", _raise)

    with pytest.raises(LoggingError) as exc_info:
        lease.quarantine("unconfirmed process-group termination")
    assert exc_info.value.code == "locking.quarantine_write_failed"

    # The write failure does not corrupt the lease itself: it can still
    # be released normally afterward, exactly as the DoD requires ("final
    # status stays FAILED", not some new, unreleasable state).
    lease.release()
    assert not quarantine_path(_git_dir(repo)).exists()

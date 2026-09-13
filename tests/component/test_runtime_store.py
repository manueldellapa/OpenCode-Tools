"""Component tests for the M08-01 runtime layout against a real filesystem.

Exercises `create_run_directory`, `allocate_run_directory`, and
`open_private_exclusive` with real `tmp_path` directories: exclusive `mkdir`
collisions, bounded regeneration, POSIX `0700`/`0600` mode enforcement, and
anti-symlink rejection (System Design SS15.2, SS15.6; ADR-008; ADR-009;
AC-021, AC-034). `run.json` schema/content and the attempt-log sink itself
are later milestones and are not exercised here.
"""

from __future__ import annotations

import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.errors import LoggingError
from opencode_tools.runlog import (
    DIRECTORY_MODE,
    FILE_MODE,
    allocate_run_directory,
    create_run_directory,
    open_private_exclusive,
)

NOW = datetime(2026, 9, 11, 14, 23, 45, 123456, tzinfo=UTC)
RUN_ID = "20260911T142345.123456Z-a1b2c3d4e5f6"


class FakeClock:
    """A `Clock` fake returning a fixed wall-clock time."""

    def __init__(self, *, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        raise NotImplementedError("runtime store tests never need monotonic time")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_create_run_directory_creates_runs_and_the_run_directory_privately(
    tmp_path: Path,
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)

    assert run_directory == tmp_path / "runs" / RUN_ID
    assert run_directory.is_dir()
    assert _mode(tmp_path / "runs") == DIRECTORY_MODE
    assert _mode(run_directory) == DIRECTORY_MODE


def test_create_run_directory_reuses_an_existing_private_runs_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "runs").mkdir(mode=DIRECTORY_MODE)

    run_directory = create_run_directory(tmp_path, RUN_ID)

    assert run_directory.is_dir()


def test_create_run_directory_fails_closed_on_a_pre_existing_looser_runs_mode(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    runs_root.chmod(0o755)  # bypass umask to force an exact, looser mode

    with pytest.raises(LoggingError) as excinfo:
        create_run_directory(tmp_path, RUN_ID)

    assert excinfo.value.code == "runlog.directory_mode_too_permissive"
    assert not (runs_root / RUN_ID).exists()
    # The pre-existing directory's mode is never repaired automatically.
    assert _mode(runs_root) == 0o755


def test_create_run_directory_fails_closed_on_a_symlinked_runs_directory(
    tmp_path: Path,
) -> None:
    real_target = tmp_path / "elsewhere"
    real_target.mkdir(mode=DIRECTORY_MODE)
    (tmp_path / "runs").symlink_to(real_target, target_is_directory=True)

    with pytest.raises(LoggingError) as excinfo:
        create_run_directory(tmp_path, RUN_ID)

    assert excinfo.value.code == "runlog.directory_is_symlink"
    assert not (real_target / RUN_ID).exists()


def test_create_run_directory_never_overwrites_an_existing_run_directory(
    tmp_path: Path,
) -> None:
    first = create_run_directory(tmp_path, RUN_ID)
    marker = first / "run.json"
    marker.write_text("original", encoding="utf-8")

    with pytest.raises(LoggingError) as excinfo:
        create_run_directory(tmp_path, RUN_ID)

    assert excinfo.value.code == "runlog.run_directory_collision"
    assert marker.read_text(encoding="utf-8") == "original"


def test_allocate_run_directory_returns_a_pattern_matching_id_and_directory(
    tmp_path: Path,
) -> None:
    run_id, run_directory = allocate_run_directory(tmp_path, FakeClock())

    assert run_directory == tmp_path / "runs" / run_id
    assert run_directory.is_dir()
    assert _mode(run_directory) == DIRECTORY_MODE


def test_allocate_run_directory_regenerates_on_a_simulated_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    first_id, first_directory = allocate_run_directory(tmp_path, clock)

    colliding_suffix = first_id.split("-")[-1]
    fresh_suffix = "b" * 12
    suffixes = iter([colliding_suffix, fresh_suffix])
    monkeypatch.setattr(secrets, "token_hex", lambda length: next(suffixes))

    second_id, second_directory = allocate_run_directory(tmp_path, clock)

    assert second_id != first_id
    assert second_directory != first_directory
    assert second_directory.is_dir()


def test_allocate_run_directory_is_bounded_and_raises_once_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    create_run_directory(tmp_path, RUN_ID)

    fixed_suffix = RUN_ID.split("-")[-1]
    attempts = 0

    def _always_colliding(length: int) -> str:
        nonlocal attempts
        attempts += 1
        return fixed_suffix

    monkeypatch.setattr(secrets, "token_hex", _always_colliding)

    with pytest.raises(LoggingError) as excinfo:
        allocate_run_directory(tmp_path, clock, max_attempts=3)

    assert excinfo.value.code == "runlog.run_directory_collision_exhausted"
    assert attempts == 3
    assert len(excinfo.value.causes) == 1
    assert excinfo.value.causes[0].code == "runlog.run_directory_collision"


def test_open_private_exclusive_creates_a_private_regular_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "architect-provider-attempt-1.log"

    file_descriptor = open_private_exclusive(path)
    try:
        os.write(file_descriptor, b"hello")
    finally:
        os.close(file_descriptor)

    assert path.read_bytes() == b"hello"
    assert _mode(path) == FILE_MODE


def test_open_private_exclusive_never_truncates_an_existing_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reviewer-cycle-1-provider-attempt-1.log"
    first_fd = open_private_exclusive(path)
    os.write(first_fd, b"original-content")
    os.close(first_fd)

    with pytest.raises(LoggingError) as excinfo:
        open_private_exclusive(path)

    assert excinfo.value.code == "runlog.artifact_file_collision"
    assert path.read_bytes() == b"original-content"


def test_open_private_exclusive_fails_closed_on_a_symlink(tmp_path: Path) -> None:
    real_target = tmp_path / "real.log"
    symlink_path = tmp_path / "coder-cycle-1-provider-attempt-1.log"
    symlink_path.symlink_to(real_target)

    with pytest.raises(LoggingError) as excinfo:
        open_private_exclusive(symlink_path)

    # A pre-existing final-component symlink is already rejected by O_EXCL
    # (POSIX: EEXIST regardless of the symlink's own contents); O_NOFOLLOW
    # is kept as defense-in-depth for call patterns that ever drop O_EXCL.
    assert excinfo.value.code == "runlog.artifact_file_collision"
    assert not real_target.exists()


def test_open_private_exclusive_rejects_a_relative_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path must be absolute"):
        open_private_exclusive(Path("relative.log"))

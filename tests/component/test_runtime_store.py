"""Component tests for the full M08 runtime store against a real filesystem.

Exercises `create_run_directory`, `allocate_run_directory`,
`open_private_exclusive`, `persist_run_record`, and `AttemptLogFileSink`
with real `tmp_path` directories: exclusive `mkdir` collisions, bounded
regeneration, POSIX `0700`/`0600` mode enforcement, anti-symlink rejection,
atomic serialize/open/write/replace with fault injection at every step, and
the append-only attempt log -- multiple distinct logs, lossless non-UTF-8
payloads, a real `ProcessRunner` (M05) draining genuinely concurrent
stdout/stderr through it unmodified, and a sink fault reaching
`RunOutcome.LOGGING_ERROR` with the child still active (System Design
SS15.2, SS15.4-SS15.6; ADR-008; ADR-009; AC-021, AC-033, AC-034). `run.json`
schema/content itself is `tests/unit/test_run_schema.py`.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import opencode_tools.runlog as runlog_module
from opencode_tools.domain import (
    AgentRole,
    PersistenceStatus,
    PipelinePhase,
    ProcessSpec,
    RunOutcome,
    RunRecord,
    TargetRepository,
    Workspace,
)
from opencode_tools.errors import LoggingError
from opencode_tools.process import SubprocessRunner
from opencode_tools.runlog import (
    DIRECTORY_MODE,
    FILE_MODE,
    AttemptLogFileSink,
    allocate_run_directory,
    attempt_log_filename,
    create_run_directory,
    open_private_exclusive,
    persist_run_record,
    serialize_run_record,
)

NOW = datetime(2026, 9, 11, 14, 23, 45, 123456, tzinfo=UTC)
RUN_ID = "20260911T142345.123456Z-a1b2c3d4e5f6"
WORKSPACE_ROOT = Path("/workspaces/opencode-tools")
HELPER = Path(__file__).resolve().parent / "helpers" / "echo_process.py"


class RealClock:
    """A `Clock` that reads genuine wall/monotonic time.

    `AttemptLogFileSink`'s header/footer timestamps and the real
    `SubprocessRunner` integration tests need actual elapsed time, not a
    fixed fake, to prove the sink works against a genuinely running child.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


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


def _run_record(*, artifact_path: Path, run_id: str = RUN_ID) -> RunRecord:
    target_root = WORKSPACE_ROOT / "backend"
    return RunRecord(
        schema_version=1,
        run_id=run_id,
        artifact_path=artifact_path,
        workspace=Workspace(root=WORKSPACE_ROOT),
        target=TargetRepository(
            root=target_root,
            workspace_relative=Path("backend"),
            git_common_dir=target_root / ".git",
        ),
        issue_number=4,
        config={},
        environment={},
        started_at=NOW,
        current_phase=PipelinePhase.PREFLIGHT,
        persistence_status=PersistenceStatus.OK,
    )


def _leftover_temp_files(run_directory: Path) -> list[Path]:
    return [path for path in run_directory.iterdir() if path.name != "run.json"]


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


def test_persist_run_record_writes_a_valid_private_document(tmp_path: Path) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"
    record = _run_record(artifact_path=document_path)

    persist_run_record(record)

    assert document_path.read_bytes() == serialize_run_record(record)
    assert _mode(document_path) == FILE_MODE
    assert _leftover_temp_files(run_directory) == []


def test_persist_run_record_atomically_replaces_the_previous_document(
    tmp_path: Path,
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"
    first = _run_record(artifact_path=document_path)
    persist_run_record(first)

    second = replace(first, current_phase=PipelinePhase.FINISHED)
    persist_run_record(second)

    assert document_path.read_bytes() == serialize_run_record(second)
    assert document_path.read_bytes() != serialize_run_record(first)
    assert _leftover_temp_files(run_directory) == []


def test_persist_run_record_leaves_the_last_valid_document_on_a_serialize_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"
    good_record = _run_record(artifact_path=document_path)
    persist_run_record(good_record)
    expected_bytes = document_path.read_bytes()

    def _raise(record: RunRecord) -> bytes:
        raise ValueError("simulated serialize fault")

    monkeypatch.setattr(runlog_module, "serialize_run_record", _raise)

    with pytest.raises(LoggingError) as excinfo:
        persist_run_record(good_record)

    assert excinfo.value.code == "runlog.run_record_serialize_failed"
    assert document_path.read_bytes() == expected_bytes
    assert _leftover_temp_files(run_directory) == []


def test_persist_run_record_leaves_the_last_valid_document_on_an_open_fault(
    tmp_path: Path,
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"
    good_record = _run_record(artifact_path=document_path)
    persist_run_record(good_record)
    expected_bytes = document_path.read_bytes()

    run_directory.chmod(0o500)  # remove write: O_CREAT on a new temp fails
    try:
        with pytest.raises(LoggingError) as excinfo:
            persist_run_record(good_record)
    finally:
        run_directory.chmod(DIRECTORY_MODE)

    assert excinfo.value.code == "runlog.artifact_file_open_failed"
    assert document_path.read_bytes() == expected_bytes


def test_persist_run_record_removes_its_temp_file_and_raises_on_a_write_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"
    record = _run_record(artifact_path=document_path)

    def _raise(file_descriptor: int) -> None:
        raise OSError("simulated disk-full fsync failure")

    monkeypatch.setattr(os, "fsync", _raise)

    with pytest.raises(LoggingError) as excinfo:
        persist_run_record(record)

    assert excinfo.value.code == "runlog.run_record_write_failed"
    assert not document_path.exists()
    assert _leftover_temp_files(run_directory) == []


def test_persist_run_record_leaves_the_last_valid_document_on_a_replace_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"
    good_record = _run_record(artifact_path=document_path)
    persist_run_record(good_record)
    expected_bytes = document_path.read_bytes()

    def _raise(source: object, destination: object) -> None:
        raise OSError("simulated cross-device replace failure")

    monkeypatch.setattr(os, "replace", _raise)

    with pytest.raises(LoggingError) as excinfo:
        persist_run_record(good_record)

    assert excinfo.value.code == "runlog.run_record_replace_failed"
    assert document_path.read_bytes() == expected_bytes
    assert _leftover_temp_files(run_directory) == []


def test_persist_run_record_tolerates_a_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"
    record = _run_record(artifact_path=document_path)

    real_fsync = os.fsync
    call_count = 0

    def _flaky_fsync(file_descriptor: int) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            real_fsync(file_descriptor)
            return
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(os, "fsync", _flaky_fsync)

    persist_run_record(record)  # must not raise: directory fsync is best effort

    assert document_path.read_bytes() == serialize_run_record(record)
    assert call_count == 2


def _read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().decode("utf-8").splitlines()]


def test_attempt_log_file_sink_creates_a_private_regular_file(tmp_path: Path) -> None:
    filename = "architect-provider-attempt-1.log"

    sink = AttemptLogFileSink(tmp_path, filename, RealClock())
    sink.close()

    assert sink.path == Path(filename)
    assert (tmp_path / filename).is_file()
    assert _mode(tmp_path / filename) == FILE_MODE


def test_attempt_log_file_sink_refuses_to_reuse_an_existing_path(
    tmp_path: Path,
) -> None:
    filename = "coder-cycle-1-provider-attempt-1.log"
    (tmp_path / filename).write_text("pre-existing", encoding="utf-8")

    with pytest.raises(LoggingError) as excinfo:
        AttemptLogFileSink(tmp_path, filename, RealClock())

    assert excinfo.value.code == "runlog.artifact_file_collision"
    assert (tmp_path / filename).read_text(encoding="utf-8") == "pre-existing"


def test_attempt_log_file_sink_writes_one_json_record_per_line(tmp_path: Path) -> None:
    filename = "reviewer-cycle-1-provider-attempt-1.log"
    sink = AttemptLogFileSink(tmp_path, filename, RealClock())

    sink.write("stdout", b"hello", datetime(2026, 9, 11, 14, 23, 45, tzinfo=UTC))
    sink.write("stderr", b"warning", datetime(2026, 9, 11, 14, 23, 46, tzinfo=UTC))
    sink.close()

    records = _read_records(tmp_path / filename)
    assert len(records) == 2
    assert records[0]["channel"] == "stdout"
    assert records[0]["timestamp"] == "2026-09-11T14:23:45.000000Z"
    assert base64.b64decode(records[0]["payload_base64"]) == b"hello"
    assert records[1]["channel"] == "stderr"
    assert base64.b64decode(records[1]["payload_base64"]) == b"warning"


def test_attempt_log_file_sink_represents_non_utf8_bytes_losslessly(
    tmp_path: Path,
) -> None:
    filename = "coder-cycle-1-provider-attempt-1.log"
    garbage = b"\xff\xfe\x00invalid-utf8\x80\xc3\x28"
    sink = AttemptLogFileSink(tmp_path, filename, RealClock())

    sink.write("stdout", garbage, datetime(2026, 9, 11, 14, 23, 45, tzinfo=UTC))
    sink.close()

    records = _read_records(tmp_path / filename)
    assert base64.b64decode(records[0]["payload_base64"]) == garbage


def test_attempt_log_file_sink_header_and_footer_are_runner_channel_records(
    tmp_path: Path,
) -> None:
    filename = "architect-provider-attempt-1.log"
    sink = AttemptLogFileSink(tmp_path, filename, RealClock())

    sink.write_header(command=("/usr/bin/git", "status"), cwd=tmp_path)
    sink.write("stdout", b"output", RealClock().now())
    sink.write_footer(outcome=RunOutcome.SUCCEEDED, duration_ns=42)
    sink.close()

    records = _read_records(tmp_path / filename)
    assert [record["channel"] for record in records] == ["runner", "stdout", "runner"]

    header = json.loads(base64.b64decode(records[0]["payload_base64"]))
    assert header == {
        "event": "header",
        "command": ["/usr/bin/git", "status"],
        "cwd": str(tmp_path),
    }

    footer = json.loads(base64.b64decode(records[2]["payload_base64"]))
    assert footer == {"event": "footer", "outcome": "SUCCEEDED", "duration_ns": 42}


def test_attempt_log_file_sink_write_footer_rejects_a_non_run_outcome(
    tmp_path: Path,
) -> None:
    filename = "architect-provider-attempt-1.log"
    sink = AttemptLogFileSink(tmp_path, filename, RealClock())
    try:
        with pytest.raises(TypeError, match="outcome must be RunOutcome"):
            sink.write_footer(outcome="SUCCEEDED", duration_ns=1)  # type: ignore[arg-type]
    finally:
        sink.close()


def test_attempt_log_file_sink_write_raises_a_plain_os_error_on_a_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filename = "coder-cycle-1-provider-attempt-1.log"
    sink = AttemptLogFileSink(tmp_path, filename, RealClock())

    def _raise(payload: bytes) -> int:
        raise OSError("simulated disk-full write failure")

    monkeypatch.setattr(sink._stream, "write", _raise)

    with pytest.raises(OSError, match="simulated disk-full"):
        sink.write("stdout", b"data", RealClock().now())


def test_attempt_log_file_sink_close_raises_on_an_fsync_fault_but_keeps_flushed_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filename = "reviewer-cycle-1-provider-attempt-1.log"
    sink = AttemptLogFileSink(tmp_path, filename, RealClock())
    sink.write("stdout", b"flushed-before-fsync-fault", RealClock().now())

    def _raise_fsync(file_descriptor: int) -> None:
        raise OSError("no space")

    monkeypatch.setattr(os, "fsync", _raise_fsync)

    with pytest.raises(LoggingError) as excinfo:
        sink.close()

    assert excinfo.value.code == "runlog.attempt_log_close_failed"
    assert (
        base64.b64decode(_read_records(tmp_path / filename)[0]["payload_base64"])
        == b"flushed-before-fsync-fault"
    )

    sink.close()  # idempotent even after a failed close


def test_attempt_log_file_sink_produces_distinct_files_per_attempt(
    tmp_path: Path,
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    first_name = attempt_log_filename(AgentRole.CODER, 1, 1)
    second_name = attempt_log_filename(AgentRole.CODER, 1, 2)

    first_sink = AttemptLogFileSink(run_directory, first_name, RealClock())
    second_sink = AttemptLogFileSink(run_directory, second_name, RealClock())
    first_sink.write("stdout", b"attempt-1", RealClock().now())
    second_sink.write("stdout", b"attempt-2", RealClock().now())
    first_sink.close()
    second_sink.close()

    assert first_name != second_name
    assert (run_directory / first_name).is_file()
    assert (run_directory / second_name).is_file()
    first_records = _read_records(run_directory / first_name)
    second_records = _read_records(run_directory / second_name)
    assert base64.b64decode(first_records[0]["payload_base64"]) == b"attempt-1"
    assert base64.b64decode(second_records[0]["payload_base64"]) == b"attempt-2"


def test_attempt_log_file_sink_integrates_with_a_real_concurrent_process_runner(
    tmp_path: Path,
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    filename = "coder-cycle-1-provider-attempt-1.log"
    sink = AttemptLogFileSink(run_directory, filename, RealClock())
    payload_size = 300_000  # well past the OS pipe buffer: forces real chunking
    spec = ProcessSpec(
        argv=(sys.executable, str(HELPER), "--large", str(payload_size)),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=30,
        termination_grace_seconds=5,
    )
    runner = SubprocessRunner(RealClock())

    result = runner.run(spec, sink=sink)
    sink.close()

    assert result.outcome == RunOutcome.SUCCEEDED
    assert result.log_path == Path(filename)
    records = _read_records(run_directory / filename)
    assert any(record["channel"] == "stdout" for record in records)
    assert any(record["channel"] == "stderr" for record in records)
    assert len(records) > 2  # proves genuine multi-chunk concurrent draining

    stdout_bytes = b"".join(
        base64.b64decode(record["payload_base64"])
        for record in records
        if record["channel"] == "stdout"
    )
    stderr_bytes = b"".join(
        base64.b64decode(record["payload_base64"])
        for record in records
        if record["channel"] == "stderr"
    )
    assert stdout_bytes == b"x" * payload_size
    assert stderr_bytes == b"x" * payload_size


def test_attempt_log_file_sink_fault_reaches_logging_error_with_an_active_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory = create_run_directory(tmp_path, RUN_ID)
    filename = "coder-cycle-1-provider-attempt-1.log"
    sink = AttemptLogFileSink(run_directory, filename, RealClock())

    real_write = sink.write
    calls = 0

    def _flaky_write(channel: Any, payload: bytes, timestamp: datetime) -> None:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise OSError("simulated disk-full write failure")
        real_write(channel, payload, timestamp)

    monkeypatch.setattr(sink, "write", _flaky_write)

    spec = ProcessSpec(
        argv=(sys.executable, str(HELPER), "--large", "300000"),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=30,
        termination_grace_seconds=5,
    )
    runner = SubprocessRunner(RealClock())

    result = runner.run(spec, sink=sink)

    assert result.outcome == RunOutcome.LOGGING_ERROR
    assert result.termination_confirmed is True

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
from typing import Any, cast

import pytest

import opencode_tools.runlog as runlog_module
from opencode_tools.config import sanitize_app_config
from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    AppConfig,
    AttemptRecord,
    ConfigSource,
    ExecutionConfig,
    FinalStatus,
    FrozenJsonValue,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    ProcessSpec,
    ProviderDiagnostic,
    ProviderRetryConfig,
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
    commit_prepared_run_record,
    create_run_directory,
    discard_prepared_run_record,
    open_private_exclusive,
    persist_run_record,
    prepare_run_record,
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


def _app_config(*, runtime_root: Path) -> AppConfig:
    return AppConfig(
        source=ConfigSource.DEFAULTS,
        execution=ExecutionConfig(
            opencode_timeout_seconds=1800,
            utility_timeout_seconds=30,
            termination_grace_seconds=5,
            max_review_cycles=3,
        ),
        provider_retry=ProviderRetryConfig(
            max_attempts=3,
            initial_delay_seconds=2,
            multiplier=2.0,
            max_delay_seconds=30,
        ),
        runtime_root=runtime_root,
    )


def _leftover_temp_files(run_directory: Path) -> list[Path]:
    return [path for path in run_directory.iterdir() if path.name != "run.json"]


def test_prepare_run_record_does_not_publish_until_commit(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    artifact_path = run_directory / "run.json"
    original = _run_record(artifact_path=artifact_path)
    persist_run_record(original)
    original_payload = artifact_path.read_bytes()

    terminal = replace(
        original,
        current_phase=PipelinePhase.FINISHED,
        final_status=FinalStatus.FAILED,
        expected_exit_code=20,
    )
    staged_path = prepare_run_record(terminal)

    assert staged_path.exists()
    assert artifact_path.read_bytes() == original_payload

    commit_prepared_run_record(terminal, staged_path)

    assert not staged_path.exists()
    assert artifact_path.read_bytes() == serialize_run_record(terminal)


def test_discard_prepared_run_record_leaves_canonical_run_json_untouched(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    artifact_path = run_directory / "run.json"
    original = _run_record(artifact_path=artifact_path)
    persist_run_record(original)
    original_payload = artifact_path.read_bytes()

    staged_path = prepare_run_record(
        replace(original, current_phase=PipelinePhase.FINISHED)
    )
    discard_prepared_run_record(staged_path)

    assert not staged_path.exists()
    assert artifact_path.read_bytes() == original_payload


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


# =============================================================================
# Release-evidence composites (issue #57, M15-01): AC-021, AC-033, AC-034
# =============================================================================
#
# Each existing test above already proves its own narrow mechanic; these
# composites additionally prove the AC's own cross-cutting claim -- multiple
# distinct dimensions (a provider retry *and* a review rework; two separate
# fault sites against the *same* prior document; mode *and* privacy) holding
# together in one golden scenario, under the AC's own canonical name.


def _ac021_probe_result(*, log_path: Path) -> ProcessResult:
    """A `git status` probe's process evidence, for a `GitCheckRecord`."""

    return ProcessResult(
        command=("/usr/bin/git", "status", "--porcelain=v2", "--branch"),
        cwd=WORKSPACE_ROOT / "backend",
        started_at=NOW,
        finished_at=NOW,
        duration_ns=500_000,
        return_code=0,
        timed_out=False,
        termination_confirmed=True,
        log_path=log_path,
        stdout_byte_count=0,
        stdout_sha256="probe-stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="probe-stderr-digest",
        outcome=RunOutcome.SUCCEEDED,
    )


def _ac021_git_check(
    *, sequence: int, purpose: str, fingerprint: str
) -> GitCheckRecord:
    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=(
            _ac021_probe_result(log_path=Path(f"git-check-{sequence}.log")),
        ),
        state=GitState(
            root=WORKSPACE_ROOT / "backend",
            branch="feat/m08-runtime-store",
            head="0123456789abcdef",
            porcelain_summary="clean",
            staged=(),
            unstaged=(),
            untracked=(),
            fingerprint=fingerprint,
        ),
        safety_status=GitSafetyStatus.SAFE,
    )


def _ac021_agent_process_result(*, log_path: Path, duration_ns: int) -> ProcessResult:
    """An agent invocation's process evidence, tied to its own attempt log."""

    return ProcessResult(
        command=("/usr/bin/opencode", "run", "--agent", "coder", "--format", "json"),
        cwd=WORKSPACE_ROOT / "backend",
        started_at=NOW,
        finished_at=NOW,
        duration_ns=duration_ns,
        return_code=0,
        timed_out=False,
        termination_confirmed=True,
        log_path=log_path,
        stdout_byte_count=6,
        stdout_sha256="agent-stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="agent-stderr-digest",
        outcome=RunOutcome.SUCCEEDED,
    )


def test_ac_021_retry_rework_logs_and_records_are_distinct(tmp_path: Path) -> None:
    """AC-021: a provider retry and a review rework each produce a distinct
    attempt-log file, and the matching `AttemptRecord`s persisted in
    `run.json` line up 1:1 with those files by role/cycle/attempt, carrying
    coherent phase, duration, return code, classifier source (provider
    diagnostic), retry decision, and outcome (System Design SS15.2-SS15.3;
    FR-043, FR-045; AC-021).

    Four provider attempts across two dimensions: `architect` cycle-less
    attempt 1; `coder` cycle 1 attempts 1-2 (a provider retry within the
    *same* logical invocation, after a `PROVIDER_ERROR`); and `coder` cycle
    2 attempt 1 (a review rework -- a *new* logical invocation opened after
    a reviewer `CHANGES_REQUIRED` in cycle 1).

    Note on schema fields: `AttemptRecord` carries no `sequence` field of
    its own (only `GitCheckRecord`/`ErrorRecord` do) -- attempt ordering is
    proven here by `RunRecord.attempts` tuple position instead, matching
    the four log files 1:1 by index. Likewise `AttemptRecord.retry_decision`
    is a plain `bool` in the current schema, not the full `RetryDecision`
    value (which is never itself persisted), so "delay pianificato" is not
    a field this document round-trips and is not asserted here.
    """

    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"

    architect_name = attempt_log_filename(AgentRole.ARCHITECT, None, 1)
    coder_retry_first_name = attempt_log_filename(AgentRole.CODER, 1, 1)
    coder_retry_second_name = attempt_log_filename(AgentRole.CODER, 1, 2)
    coder_rework_name = attempt_log_filename(AgentRole.CODER, 2, 1)
    filenames = (
        architect_name,
        coder_retry_first_name,
        coder_retry_second_name,
        coder_rework_name,
    )
    assert len(set(filenames)) == 4  # every attempt-log filename is distinct

    durations: tuple[int, int, int, int] = (1_000_000, 2_000_000, 3_000_000, 4_000_000)
    for filename, duration_ns in zip(filenames, durations, strict=True):
        sink = AttemptLogFileSink(run_directory, filename, RealClock())
        sink.write_header(command=("/usr/bin/opencode", "run"), cwd=run_directory)
        sink.write("stdout", filename.encode("ascii"), RealClock().now())
        sink.write_footer(outcome=RunOutcome.SUCCEEDED, duration_ns=duration_ns)
        sink.close()

    for filename, duration_ns in zip(filenames, durations, strict=True):
        assert (run_directory / filename).is_file()
        records = _read_records(run_directory / filename)
        footer = json.loads(base64.b64decode(records[-1]["payload_base64"]))
        assert footer == {
            "event": "footer",
            "outcome": "SUCCEEDED",
            "duration_ns": duration_ns,
        }

    architect_result = AgentResult(
        role=AgentRole.ARCHITECT,
        phase=PipelinePhase.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        process=_ac021_agent_process_result(
            log_path=Path(architect_name), duration_ns=durations[0]
        ),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.ARCHITECT,
            body="Architecture plan ready.",
            agent_status=AgentStatus.READY,
        ),
        session_id="session-architect",
        verified_agent="architect",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )
    coder_retry_first_result = AgentResult(
        role=AgentRole.CODER,
        phase=PipelinePhase.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_ac021_agent_process_result(
            log_path=Path(coder_retry_first_name), duration_ns=durations[1]
        ),
        terminal_response=None,
        session_id=None,
        verified_agent=None,
        provider_diagnostic=ProviderDiagnostic(
            source="opencode-cli",
            signature="provider_rate_limited",
            retryable=True,
            status_code=429,
            code="rate_limited",
        ),
        outcome=RunOutcome.PROVIDER_ERROR,
    )
    coder_retry_second_result = AgentResult(
        role=AgentRole.CODER,
        phase=PipelinePhase.CODER,
        review_cycle=1,
        provider_attempt=2,
        process=_ac021_agent_process_result(
            log_path=Path(coder_retry_second_name), duration_ns=durations[2]
        ),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.CODER,
            body="Implemented the requested change.",
            agent_status=AgentStatus.COMPLETED,
        ),
        session_id="session-coder-1",
        verified_agent="coder",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )
    coder_rework_result = AgentResult(
        role=AgentRole.CODER,
        phase=PipelinePhase.CODER,
        review_cycle=2,
        provider_attempt=1,
        process=_ac021_agent_process_result(
            log_path=Path(coder_rework_name), duration_ns=durations[3]
        ),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.CODER,
            body="Addressed the reviewer's feedback.",
            agent_status=AgentStatus.COMPLETED,
        ),
        session_id="session-coder-2",
        verified_agent="coder",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )

    attempts = (
        AttemptRecord(
            logical_invocation_id="architect",
            role=AgentRole.ARCHITECT,
            review_cycle=None,
            provider_attempt=1,
            git_before=_ac021_git_check(
                sequence=0, purpose="architect-before", fingerprint="fp-0"
            ),
            git_after=_ac021_git_check(
                sequence=1, purpose="architect-after", fingerprint="fp-0"
            ),
            agent_result=architect_result,
            retry_decision=False,
        ),
        AttemptRecord(
            logical_invocation_id="coder-cycle-1",
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=1,
            git_before=_ac021_git_check(
                sequence=2, purpose="coder-cycle-1-attempt-1-before", fingerprint="fp-1"
            ),
            git_after=_ac021_git_check(
                sequence=3, purpose="coder-cycle-1-attempt-1-after", fingerprint="fp-1"
            ),
            agent_result=coder_retry_first_result,
            # a provider retry was decided right after this failed attempt.
            retry_decision=True,
        ),
        AttemptRecord(
            logical_invocation_id="coder-cycle-1",
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=2,
            git_before=_ac021_git_check(
                sequence=4, purpose="coder-cycle-1-attempt-2-before", fingerprint="fp-1"
            ),
            git_after=_ac021_git_check(
                sequence=5, purpose="coder-cycle-1-attempt-2-after", fingerprint="fp-2"
            ),
            agent_result=coder_retry_second_result,
            retry_decision=False,
        ),
        AttemptRecord(
            logical_invocation_id="coder-cycle-2",
            role=AgentRole.CODER,
            review_cycle=2,
            provider_attempt=1,
            git_before=_ac021_git_check(
                sequence=6, purpose="coder-cycle-2-attempt-1-before", fingerprint="fp-2"
            ),
            git_after=_ac021_git_check(
                sequence=7, purpose="coder-cycle-2-attempt-1-after", fingerprint="fp-3"
            ),
            agent_result=coder_rework_result,
            retry_decision=False,
        ),
    )
    record = replace(_run_record(artifact_path=document_path), attempts=attempts)

    persist_run_record(record)

    assert document_path.read_bytes() == serialize_run_record(record)
    parsed = json.loads(document_path.read_bytes())
    persisted_attempts = parsed["attempts"]
    assert len(persisted_attempts) == 4

    expected: list[
        tuple[str, str, int | None, int, str, int, str | None, bool, str]
    ] = [
        (
            "architect",
            "ARCHITECT",
            None,
            1,
            architect_name,
            durations[0],
            None,
            False,
            "SUCCEEDED",
        ),
        (
            "coder-cycle-1",
            "CODER",
            1,
            1,
            coder_retry_first_name,
            durations[1],
            "opencode-cli",
            True,
            "PROVIDER_ERROR",
        ),
        (
            "coder-cycle-1",
            "CODER",
            1,
            2,
            coder_retry_second_name,
            durations[2],
            None,
            False,
            "SUCCEEDED",
        ),
        (
            "coder-cycle-2",
            "CODER",
            2,
            1,
            coder_rework_name,
            durations[3],
            None,
            False,
            "SUCCEEDED",
        ),
    ]

    for persisted, (
        logical_invocation_id,
        role,
        review_cycle,
        provider_attempt,
        log_path,
        duration_ns,
        provider_diagnostic_source,
        retry_decision,
        outcome,
    ) in zip(persisted_attempts, expected, strict=True):
        agent_result = persisted["agent_result"]
        assert persisted["logical_invocation_id"] == logical_invocation_id
        assert persisted["role"] == role
        assert persisted["review_cycle"] == review_cycle
        assert persisted["provider_attempt"] == provider_attempt
        assert agent_result["phase"] == (
            "ARCHITECT" if role == "ARCHITECT" else "CODER"
        )
        assert agent_result["process"]["log_path"] == log_path
        assert agent_result["process"]["duration_ns"] == duration_ns
        assert agent_result["process"]["return_code"] == 0
        diagnostic = agent_result["provider_diagnostic"]
        if provider_diagnostic_source is None:
            assert diagnostic is None
        else:
            assert diagnostic is not None
            assert diagnostic["source"] == provider_diagnostic_source
        assert persisted["retry_decision"] == retry_decision
        assert agent_result["outcome"] == outcome

    # every attempt's log filename is distinct and lines up 1:1, by position,
    # with the four attempt-log files actually written above.
    persisted_log_paths = [
        attempt["agent_result"]["process"]["log_path"] for attempt in persisted_attempts
    ]
    assert persisted_log_paths == list(filenames)
    assert len(set(persisted_log_paths)) == 4


def test_ac_033_log_and_atomic_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-033: fault injection on either opening the replacement temp file
    or on the atomic `os.replace` never corrupts the last valid `run.json`
    -- each fault raises `LoggingError` with `outcome is
    RunOutcome.LOGGING_ERROR` and leaves the previously persisted document,
    and the run directory, exactly as they were (System Design SS15.4;
    FR-044, FR-052; AC-033).

    Both fault sites run in sequence against the *same* prior document, one
    right after the other, reusing this file's own established injection
    idioms (`chmod` for the open fault, a monkeypatched `os.replace` for the
    replace fault) rather than re-deriving them.

    Out of scope here, deliberately: "interrompe nuove invocazioni", a
    non-zero process exit code, and an "artifact may be incomplete" stderr
    warning are CLI/orchestrator-level behaviors this low-level runlog
    component test has no CLI or orchestrator to exercise. Those are
    already proven by
    `tests/unit/test_cli.py::test_render_issue_result_warns_when_final_persistence_failed`
    and by `tests/component/test_pipeline_failures.py`'s
    first-persist-failure tests.
    """

    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"
    good_record = _run_record(artifact_path=document_path)
    persist_run_record(good_record)
    expected_bytes = document_path.read_bytes()

    # -- fault 1: opening the replacement temp file fails (no write access).
    run_directory.chmod(0o500)
    try:
        with pytest.raises(LoggingError) as excinfo:
            persist_run_record(good_record)
    finally:
        run_directory.chmod(DIRECTORY_MODE)

    assert excinfo.value.code == "runlog.artifact_file_open_failed"
    assert excinfo.value.outcome is RunOutcome.LOGGING_ERROR
    assert document_path.read_bytes() == expected_bytes
    assert _leftover_temp_files(run_directory) == []

    # -- fault 2: the atomic os.replace onto run.json fails.
    def _raise_on_replace(source: object, destination: object) -> None:
        raise OSError("simulated cross-device replace failure")

    monkeypatch.setattr(os, "replace", _raise_on_replace)

    with pytest.raises(LoggingError) as excinfo:
        persist_run_record(good_record)

    assert excinfo.value.code == "runlog.run_record_replace_failed"
    assert excinfo.value.outcome is RunOutcome.LOGGING_ERROR
    assert document_path.read_bytes() == expected_bytes
    assert _leftover_temp_files(run_directory) == []


def test_ac_034_posix_modes_and_private_serialization(tmp_path: Path) -> None:
    """AC-034: every runtime-store artifact is created at mode `0700`
    (directories) or `0600` (files), and the persisted `run.json` never
    leaks a credential or a raw prompt through its `config`/`environment`
    fields (System Design SS15.6; ADR-008; FR-046; AC-034).

    `AppConfig` structurally has no credential-shaped field to leak in the
    first place (no model/provider ID, no token) -- `sanitize_app_config`
    only flattens it into JSON-native primitives -- so, like
    `tests/unit/test_run_schema.py`'s equivalent unit-level check, the
    negative assertion below is defense in depth over the real,
    filesystem-persisted bytes rather than proof of an active stripping
    step. Similarly, `opencode.build_run_spec` keeps the agent prompt off
    argv entirely (it travels only on stdin), so no `command` this store
    ever persists -- via `RunRecord`/`ProcessResult.command` or
    `AttemptLogFileSink.write_header` -- can carry raw prompt text either;
    proving that structural fact belongs to `opencode.py`'s own tests, not
    this runlog-only component test.
    """

    run_directory = create_run_directory(tmp_path, RUN_ID)
    document_path = run_directory / "run.json"

    sanitized_config = sanitize_app_config(
        _app_config(runtime_root=WORKSPACE_ROOT / ".opencode-tools")
    )
    environment = {
        "tool_version": "0.1.0",
        "python_version": "3.13.15",
        "platform": "Darwin",
        "opencode_version": "1.17.18",
        "git_version": "2.43.0",
        "gh_version": "2.40.0",
    }
    record = replace(
        _run_record(artifact_path=document_path),
        config=cast(dict[str, FrozenJsonValue], sanitized_config),
        environment=environment,
    )

    persist_run_record(record)

    filename = attempt_log_filename(AgentRole.ARCHITECT, None, 1)
    sink = AttemptLogFileSink(run_directory, filename, RealClock())
    sink.write_header(command=("/usr/bin/opencode", "run"), cwd=run_directory)
    sink.close()

    # -- POSIX modes: 0700 for every directory, 0600 for every file.
    assert _mode(tmp_path / "runs") == DIRECTORY_MODE
    assert _mode(run_directory) == DIRECTORY_MODE
    assert _mode(document_path) == FILE_MODE
    assert _mode(run_directory / filename) == FILE_MODE

    # -- private serialization: no credential, secret, or raw environment
    # dump anywhere in the persisted document.
    text = document_path.read_bytes().decode("utf-8").lower()
    for forbidden in ("credential", "password", "secret", "api_key", "authorization"):
        assert forbidden not in text
    assert json.loads(document_path.read_bytes())["environment"] == environment

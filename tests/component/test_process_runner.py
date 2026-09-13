"""Component tests for `SubprocessRunner` against real local child processes.

These tests spawn `tests/component/helpers/echo_process.py` through the real
Python interpreter (`sys.executable`), never a fixture or mock, to prove the
POSIX spawn boundary itself: no shell, no `os.chdir()`, stdin delivered
out-of-band from argv/environment, cwd honored per child, and a technically
faithful `ProcessResult` for both the success and the process-error paths
(AC-014 partial, AC-015). Timeout/termination-escalation and concurrent
bounded draining are later milestones (M05-02/M05-03) and are not exercised
here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import ProcessSpec, RunOutcome
from opencode_tools.process import SubprocessRunner

HELPER = Path(__file__).resolve().parent / "helpers" / "echo_process.py"


class FakeClock:
    """A `Clock` fake with a fixed wall-clock time and a step counter."""

    def __init__(self, *, now: datetime, start_ns: int = 0) -> None:
        self._now = now
        self._monotonic_ns = start_ns

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        self._monotonic_ns += 1
        return self._monotonic_ns


class RecordingAttemptLogSink:
    """An `AttemptLogSink` fake that records writes and exposes a fixed path."""

    def __init__(self, path: Path = Path("attempt.log")) -> None:
        self._path = path
        self.writes: list[tuple[str, bytes, datetime]] = []
        self.closed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: str, payload: bytes, timestamp: datetime) -> None:
        self.writes.append((channel, payload, timestamp))

    def close(self) -> None:
        self.closed = True


NOW = datetime(2026, 9, 13, 8, 0, 0, tzinfo=UTC)


def _spec(
    argv: tuple[str, ...],
    cwd: Path,
    *,
    stdin: str | bytes | None = None,
    environment_overrides: dict[str, str] | None = None,
) -> ProcessSpec:
    return ProcessSpec(
        argv=argv,
        cwd=cwd,
        stdin=stdin,
        timeout_seconds=30.0,
        termination_grace_seconds=5.0,
        environment_overrides=environment_overrides or {},
    )


def _helper_argv(*flags: str) -> tuple[str, ...]:
    return (sys.executable, str(HELPER), *flags)


def test_run_returns_succeeded_with_the_exact_command_and_cwd(tmp_path: Path) -> None:
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink(path=Path("architect-provider-attempt-1.log"))
    spec = _spec(_helper_argv(), tmp_path, stdin=None)

    result = runner.run(spec, sink=sink)

    assert result.outcome is RunOutcome.SUCCEEDED
    assert result.return_code == 0
    assert result.command == spec.argv
    assert result.cwd == tmp_path
    assert result.timed_out is False
    assert result.termination_confirmed is True
    assert result.log_path == Path("architect-provider-attempt-1.log")


def test_run_reports_nonzero_exit_as_process_error_not_provider_retryable(
    tmp_path: Path,
) -> None:
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    spec = _spec(_helper_argv("--exit-code", "7", "--stderr", "boom"), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.outcome is RunOutcome.PROCESS_ERROR
    assert result.return_code == 7
    stderr_writes = [
        payload for channel, payload, _ in sink.writes if channel == "stderr"
    ]
    assert stderr_writes == [b"boom\n"]


def test_run_reports_a_missing_executable_as_process_error_with_null_return_code(
    tmp_path: Path,
) -> None:
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    missing_executable = tmp_path / "does-not-exist"
    spec = _spec((str(missing_executable),), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.return_code is None
    assert result.outcome is RunOutcome.PROCESS_ERROR
    assert result.termination_confirmed is True
    assert result.stdout_byte_count == 0
    assert result.stderr_byte_count == 0
    assert result.stdout_sha256 == hashlib.sha256(b"").hexdigest()
    assert sink.writes == []


def test_run_delivers_stdin_out_of_band_and_never_leaks_it_into_the_command(
    tmp_path: Path,
) -> None:
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    prompt = "trusted stdin payload, never an argv token"
    spec = _spec(_helper_argv(), tmp_path, stdin=prompt)

    result = runner.run(spec, sink=sink)

    assert result.return_code == 0
    stdout_writes = [
        payload for channel, payload, _ in sink.writes if channel == "stdout"
    ]
    assert stdout_writes == [prompt.encode("utf-8")]
    assert result.stdout_byte_count == len(prompt.encode("utf-8"))
    assert result.stdout_sha256 == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert all(prompt not in token for token in result.command)


def test_run_passes_bytes_stdin_verbatim(tmp_path: Path) -> None:
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    payload = b"\x00binary-ish stdin\x01"
    spec = _spec(_helper_argv(), tmp_path, stdin=payload)

    result = runner.run(spec, sink=sink)

    assert result.return_code == 0
    stdout_writes = [chunk for channel, chunk, _ in sink.writes if channel == "stdout"]
    assert stdout_writes == [payload]


def test_run_honors_the_per_child_cwd_without_a_global_chdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbidden_chdir(path: object) -> None:
        raise AssertionError("SubprocessRunner must never call os.chdir()")

    monkeypatch.setattr(os, "chdir", _forbidden_chdir)
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    child_dir = tmp_path / "child-workspace"
    child_dir.mkdir()
    spec = _spec(_helper_argv("--print-cwd"), child_dir)

    result = runner.run(spec, sink=sink)

    assert result.return_code == 0
    stdout_writes = [
        payload for channel, payload, _ in sink.writes if channel == "stdout"
    ]
    reported_cwd = stdout_writes[0].decode("utf-8").strip()
    assert Path(reported_cwd) == child_dir.resolve()


def test_run_never_lets_a_shell_interpret_argv_metacharacters(tmp_path: Path) -> None:
    """If a shell ever interpreted this argument, `$(...)` would be
    substituted and `;` would end the command early; `shell=False` means it
    reaches the child, and this helper's stderr, as one literal token."""

    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    payload = "$(echo hijacked); rm -rf / #"
    spec = _spec(_helper_argv("--stderr", payload), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.return_code == 0
    stderr_writes = [
        payload_bytes
        for channel, payload_bytes, _ in sink.writes
        if channel == "stderr"
    ]
    assert stderr_writes == [f"{payload}\n".encode()]


def test_run_inherits_the_parent_environment_and_applies_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCT_TEST_INHERITED", "inherited-value")
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    spec = _spec(
        _helper_argv(
            "--echo-env",
            "OCT_TEST_INHERITED",
            "--echo-env",
            "OCT_TEST_OVERRIDE",
        ),
        tmp_path,
        environment_overrides={"OCT_TEST_OVERRIDE": "override-value"},
    )

    result = runner.run(spec, sink=sink)

    assert result.return_code == 0
    stdout_writes = [
        payload for channel, payload, _ in sink.writes if channel == "stdout"
    ]
    reported = stdout_writes[0].decode("utf-8").splitlines()
    assert reported == ["inherited-value", "override-value"]
    assert "OCT_TEST_OVERRIDE" not in os.environ


def test_run_environment_overrides_win_over_an_existing_parent_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`environment_overrides` must forcibly replace a value the parent
    process already has, not merely add a name the parent lacks (System
    Design SS10.3 forces e.g. `OPENCODE_AUTO_SHARE=false` this way)."""

    monkeypatch.setenv("OCT_TEST_PRECEDENCE", "parent-value")
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    spec = _spec(
        _helper_argv("--echo-env", "OCT_TEST_PRECEDENCE"),
        tmp_path,
        environment_overrides={"OCT_TEST_PRECEDENCE": "override-wins"},
    )

    result = runner.run(spec, sink=sink)

    assert result.return_code == 0
    stdout_writes = [
        payload for channel, payload, _ in sink.writes if channel == "stdout"
    ]
    assert stdout_writes[0].decode("utf-8").strip() == "override-wins"
    assert os.environ["OCT_TEST_PRECEDENCE"] == "parent-value"


def test_run_spawns_the_real_argv_but_records_the_sanitized_command(
    tmp_path: Path,
) -> None:
    """The credential must reach the real child (or the invocation would
    fail to authenticate) while `ProcessResult.command` must never contain
    it (System Design SS18.3): these are two distinct variables in
    `SubprocessRunner.run()`, and this proves neither leaked into the
    other's place."""

    credential_url = (
        "https://x-access-token:super-secret-token@github.com/example/repo.git"
    )
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    spec = _spec(_helper_argv("--stderr", credential_url), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.return_code == 0
    stderr_writes = [
        payload for channel, payload, _ in sink.writes if channel == "stderr"
    ]
    assert stderr_writes == [f"{credential_url}\n".encode()]

    assert result.command[-1] == "https://REDACTED@github.com/example/repo.git"
    assert "super-secret-token" not in result.command[-1]
    assert "x-access-token" not in result.command[-1]


def test_run_never_enumerates_or_persists_the_environment_into_the_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCT_TEST_SECRET", "super-secret-value")
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    spec = _spec(_helper_argv(), tmp_path)

    result = runner.run(spec, sink=sink)

    field_names = {field.name for field in dataclasses.fields(result)}
    assert "environment" not in field_names
    for value in dataclasses.asdict(result).values():
        assert "super-secret-value" not in repr(value)


def test_run_uses_the_injected_clock_for_timestamps_and_duration(
    tmp_path: Path,
) -> None:
    clock = FakeClock(now=NOW, start_ns=100)
    runner = SubprocessRunner(clock)
    sink = RecordingAttemptLogSink()
    spec = _spec(_helper_argv(), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.started_at == NOW
    assert result.finished_at == NOW
    assert result.duration_ns == 1

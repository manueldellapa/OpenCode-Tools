"""Component tests for `SubprocessRunner` against real local child processes.

These tests spawn `tests/component/helpers/echo_process.py` through the real
Python interpreter (`sys.executable`), never a fixture or mock, to prove the
POSIX spawn boundary itself: no shell, no `os.chdir()`, stdin delivered
out-of-band from argv/environment, cwd honored per child, concurrent bounded
draining of large simultaneous stdout/stderr without deadlock, deadline-based
timeout and process-group `SIGTERM`/`SIGKILL` escalation reaching descendants
(but not one that escaped the group), SIGINT/SIGTERM cancellation (SH-001),
and a technically faithful `ProcessResult` for the success, process-error,
timeout, and interrupted paths (AC-014, AC-015, AC-032).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import ProcessSpec, RunOutcome
from opencode_tools.process import SubprocessRunner
from opencode_tools.runlog import AttemptLogFileSink

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


class RealClock:
    """A `Clock` that reads genuine wall/monotonic time.

    The deadline/escalation tests below need `run()`'s internal polling
    loop to track *real* elapsed time against a real hanging child --
    `FakeClock`'s instant step counter would never reach a deadline
    computed in real seconds within a reasonable number of polls.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


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


class FaultingAttemptLogSink:
    """An `AttemptLogSink` fake whose `write()` raises `OSError` once at
    least `fail_after` writes have already succeeded, simulating a
    disk-full or permission failure partway through draining (M05-04)."""

    def __init__(
        self, *, path: Path = Path("attempt.log"), fail_after: int = 0
    ) -> None:
        self._path = path
        self._fail_after = fail_after
        self.writes: list[tuple[str, bytes, datetime]] = []
        self.closed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: str, payload: bytes, timestamp: datetime) -> None:
        if len(self.writes) >= self._fail_after:
            raise OSError("simulated sink write fault (disk full)")
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


def _bounded_spec(
    argv: tuple[str, ...],
    cwd: Path,
    *,
    timeout_seconds: float = 0.2,
    termination_grace_seconds: float = 0.2,
) -> ProcessSpec:
    return ProcessSpec(
        argv=argv,
        cwd=cwd,
        stdin=None,
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
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


def test_ac_015_spawn_and_non_provider_exit_are_process_error(
    tmp_path: Path,
) -> None:
    """AC-015: neither a spawn failure nor a non-zero, non-provider exit is
    ever provider-retryable -- both are `PROCESS_ERROR`, a purely technical
    outcome `process.py` can emit on its own without knowing anything about
    provider classification (its own module docstring). `PROVIDER_ERROR` is
    architecturally impossible to reach from this module; the explicit
    `is not RunOutcome.PROVIDER_ERROR` assertions below make that guarantee
    visible in the test itself rather than only implied by outcome
    exclusivity."""

    runner = SubprocessRunner(FakeClock(now=NOW))

    # Sub-scenario 1: spawn failure (the executable itself does not exist).
    spawn_sink = RecordingAttemptLogSink()
    missing_executable = tmp_path / "does-not-exist"
    spawn_spec = _spec((str(missing_executable),), tmp_path)

    spawn_result = runner.run(spawn_spec, sink=spawn_sink)

    assert spawn_result.outcome is not RunOutcome.PROVIDER_ERROR
    assert spawn_result.outcome is RunOutcome.PROCESS_ERROR
    assert spawn_result.return_code is None
    assert spawn_sink.writes == []

    # Sub-scenario 2: the child spawns fine but exits non-zero for a reason
    # that has nothing to do with the (agent-only) provider concept.
    exit_sink = RecordingAttemptLogSink()
    exit_spec = _spec(_helper_argv("--exit-code", "7", "--stderr", "boom"), tmp_path)

    exit_result = runner.run(exit_spec, sink=exit_sink)

    assert exit_result.outcome is not RunOutcome.PROVIDER_ERROR
    assert exit_result.outcome is RunOutcome.PROCESS_ERROR
    assert exit_result.return_code == 7


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


def test_run_drains_large_simultaneous_stdout_and_stderr_without_deadlock(
    tmp_path: Path,
) -> None:
    """Writing this much to both streams overflows the OS pipe buffer many
    times over; a runner that reads them one at a time (rather than with a
    dedicated concurrent reader per stream) would deadlock here instead of
    returning (System Design SS10.1/AC-014)."""

    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    size = 2_000_000
    spec = _spec(_helper_argv("--large", str(size)), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.return_code == 0
    assert result.outcome is RunOutcome.SUCCEEDED
    assert result.stdout_byte_count == size
    assert result.stderr_byte_count == size

    stdout_chunks = [
        payload for channel, payload, _ in sink.writes if channel == "stdout"
    ]
    stderr_chunks = [
        payload for channel, payload, _ in sink.writes if channel == "stderr"
    ]
    assert sum(len(chunk) for chunk in stdout_chunks) == size
    assert sum(len(chunk) for chunk in stderr_chunks) == size
    assert hashlib.sha256(b"".join(stdout_chunks)).hexdigest() == result.stdout_sha256
    assert hashlib.sha256(b"".join(stderr_chunks)).hexdigest() == result.stderr_sha256

    # Forwarding is incremental, not one giant post-hoc write of the whole
    # buffer: with a 64 KiB chunk size and 2 MB of output, many chunks land.
    assert len(stdout_chunks) > 1
    assert len(stderr_chunks) > 1


def test_run_preserves_full_partial_output_even_when_the_child_then_fails(
    tmp_path: Path,
) -> None:
    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    size = 500_000
    spec = _spec(_helper_argv("--large", str(size), "--exit-code", "9"), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.return_code == 9
    assert result.outcome is RunOutcome.PROCESS_ERROR
    assert result.stdout_byte_count == size
    assert result.stderr_byte_count == size
    assert result.termination_confirmed is True


def test_run_returns_within_the_grace_bound_when_a_descendant_holds_a_pipe_open(
    tmp_path: Path,
) -> None:
    """A detached descendant that inherits the stdout/stderr pipes and holds
    them open must not block `run()` past `termination_grace_seconds`;
    actually killing that descendant is M05-03's job (process-group
    signaling) -- until then, the honest answer is an early return with
    `termination_confirmed=False` (System Design SS10.2), never a hang."""

    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    spec = ProcessSpec(
        argv=_helper_argv("--stderr", "before-the-fork", "--fork-hold", "5"),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=30.0,
        termination_grace_seconds=0.2,
    )

    started = time.monotonic()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert result.return_code == 0
    assert result.outcome is RunOutcome.PROCESS_ERROR
    assert result.termination_confirmed is False
    stderr_chunks = [
        payload for channel, payload, _ in sink.writes if channel == "stderr"
    ]
    assert stderr_chunks == [b"before-the-fork\n"]


def test_run_join_bound_is_governed_by_termination_grace_seconds(
    tmp_path: Path,
) -> None:
    """A larger grace bound must still let `run()` return promptly once the
    descendant actually exits well within it -- the bound is a ceiling on
    how long `run()` waits for a stuck reader, not a mandatory delay."""

    runner = SubprocessRunner(FakeClock(now=NOW))
    sink = RecordingAttemptLogSink()
    spec = ProcessSpec(
        argv=_helper_argv(),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=30.0,
        termination_grace_seconds=10.0,
    )

    started = time.monotonic()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert result.return_code == 0
    assert result.termination_confirmed is True


def test_run_uses_the_injected_clock_for_timestamps_and_duration(
    tmp_path: Path,
) -> None:
    """`started_at`/`finished_at` come straight from the fake, proving the
    clock is genuinely injected. `duration_ns` can only be asserted as
    positive, not to an exact step count: since M05-03, `run()` polls
    `clock.monotonic_ns()` once per deadline check while waiting for the
    real child to exit, and that poll count depends on real scheduling."""

    clock = FakeClock(now=NOW, start_ns=100)
    runner = SubprocessRunner(clock)
    sink = RecordingAttemptLogSink()
    spec = _spec(_helper_argv(), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.started_at == NOW
    assert result.finished_at == NOW
    assert result.duration_ns >= 1


def test_run_times_out_a_hanging_child_and_confirms_its_termination(
    tmp_path: Path,
) -> None:
    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = _bounded_spec(_helper_argv("--sleep", "10"), tmp_path)

    started = time.monotonic()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert result.outcome is RunOutcome.TIMEOUT
    assert result.timed_out is True
    assert result.termination_confirmed is True
    assert result.return_code is not None
    assert result.return_code < 0  # exited by a signal, not its own return


def test_run_never_treats_a_timeout_as_a_provider_retry(tmp_path: Path) -> None:
    """AC-015's sibling guarantee for timeouts (ADR-004): TIMEOUT is never
    classified as PROVIDER_ERROR, so it can never enter the provider retry
    loop regardless of what retry.decide_retry later does with it."""

    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = _bounded_spec(_helper_argv("--sleep", "10"), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.outcome is not RunOutcome.PROVIDER_ERROR


def test_run_escalates_to_sigkill_when_the_child_ignores_sigterm(
    tmp_path: Path,
) -> None:
    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = _bounded_spec(_helper_argv("--ignore-sigterm", "--sleep", "10"), tmp_path)

    started = time.monotonic()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert result.outcome is RunOutcome.TIMEOUT
    assert result.termination_confirmed is True
    assert result.return_code == -signal.SIGKILL


def test_run_kills_the_whole_process_group_including_descendants(
    tmp_path: Path,
) -> None:
    """A descendant that stays in the child's process group (plain
    `fork()`, no `setsid()`) must be reaped by the same `killpg` that
    terminates the direct child -- otherwise its held-open pipe would
    prevent a confirmed termination."""

    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = _bounded_spec(_helper_argv("--fork-and-sleep", "10"), tmp_path)

    started = time.monotonic()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert result.outcome is RunOutcome.TIMEOUT
    assert result.termination_confirmed is True


def test_ac_014_timeout_kills_process_group_and_keeps_output(
    tmp_path: Path,
) -> None:
    """AC-014, combined in one place: a child with a same-group descendant
    that overruns its deadline is killed -- whole group, descendant
    included -- within the configured grace policy, is reported as
    `TIMEOUT` with confirmed termination, and none of the output it managed
    to write before the hang began is lost."""

    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = _bounded_spec(
        _helper_argv("--stderr", "before-the-hang", "--fork-and-sleep", "10"),
        tmp_path,
    )

    started = time.monotonic()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert result.outcome is RunOutcome.TIMEOUT
    assert result.termination_confirmed is True
    # The field's declared type is Optional[int] (see
    # test_run_reports_a_missing_executable_as_process_error_with_null_return_code
    # for the `None` case, which arises from a spawn failure, not a
    # TIMEOUT): a confirmed process-group kill always yields a real
    # negative-signal return code, per build_process_result's own contract.
    assert result.return_code is not None
    assert isinstance(result.return_code, int)
    stderr_chunks = [
        payload for channel, payload, _ in sink.writes if channel == "stderr"
    ]
    assert stderr_chunks == [b"before-the-hang\n"]


def test_run_reports_unconfirmed_termination_when_a_descendant_escapes_the_group(
    tmp_path: Path,
) -> None:
    """A descendant that calls its own `os.setsid()` leaves the child's
    process group; `killpg` on that group terminates the direct child fine,
    but can never reach the escaped descendant, which keeps holding a pipe
    open -- the honest, documented outcome is `termination_confirmed=False`
    (System Design SS10.2), never a hang and never a false `True`."""

    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = _bounded_spec(_helper_argv("--detach-fork-hold", "10"), tmp_path)

    started = time.monotonic()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert result.outcome is RunOutcome.TIMEOUT
    assert result.termination_confirmed is False


def test_footer_stays_last_when_a_descendant_keeps_the_pipe_open(
    tmp_path: Path,
) -> None:
    """Codex P2 on #133: a detached descendant can outlive the direct child
    and keep stdout/stderr open beyond the bounded reader joins. Once
    `run()` returns, surviving readers must be drain-only so the caller's
    runner footer is guaranteed to remain the final attempt-log record."""

    runner = SubprocessRunner(RealClock())
    sink = AttemptLogFileSink(tmp_path, "attempt.log", RealClock())
    late_payload = b"late descendant output\n"
    child_script = (
        "import os,time\n"
        "pid=os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    time.sleep(0.35)\n"
        f"    os.write(1, {late_payload!r})\n"
        "    os._exit(0)\n"
        "os._exit(0)\n"
    )
    spec = ProcessSpec(
        argv=(sys.executable, "-c", child_script),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=30.0,
        termination_grace_seconds=0.05,
    )

    sink.write_header(command=spec.argv, cwd=spec.cwd)
    result = runner.run(spec, sink=sink)

    assert result.return_code == 0
    assert result.termination_confirmed is False
    assert result.outcome is RunOutcome.PROCESS_ERROR

    sink.write_footer(outcome=result.outcome, duration_ns=result.duration_ns)
    time.sleep(0.5)
    sink.close()

    records = [
        json.loads(line)
        for line in (tmp_path / "attempt.log").read_text(encoding="utf-8").splitlines()
    ]
    last_record = records[-1]
    assert last_record["channel"] == "runner"
    assert json.loads(base64.b64decode(last_record["payload_base64"]))["event"] == "footer"
    assert all(
        base64.b64decode(record["payload_base64"]) != late_payload
        for record in records
        if record["channel"] == "stdout"
    )


def test_run_applies_the_same_escalation_on_sigint_cancellation(
    tmp_path: Path,
) -> None:
    """SH-001: an incoming SIGINT while a child is active reuses the exact
    same bounded escalation as a timeout, reported as INTERRUPTED rather
    than TIMEOUT, and still returns well before the child's own sleep."""

    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = ProcessSpec(
        argv=_helper_argv("--sleep", "10"),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=30.0,
        termination_grace_seconds=0.2,
    )

    def _send_sigint_shortly() -> None:
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGINT)

    canceller = threading.Thread(target=_send_sigint_shortly, daemon=True)
    started = time.monotonic()
    canceller.start()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started
    canceller.join(timeout=2.0)

    assert elapsed < 3.0
    assert result.outcome is RunOutcome.INTERRUPTED
    assert result.timed_out is False
    assert result.termination_confirmed is True


def test_run_restores_the_previous_signal_handlers_after_returning(
    tmp_path: Path,
) -> None:
    """Installing SIGINT/SIGTERM handlers for the duration of one child's
    execution must not leak into the rest of the process: whatever handler
    was registered before `run()` is called must be back in place after."""

    sentinel_sigterm = signal.getsignal(signal.SIGTERM)
    sentinel_sigint = signal.getsignal(signal.SIGINT)

    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = _spec(_helper_argv(), tmp_path)

    runner.run(spec, sink=sink)

    assert signal.getsignal(signal.SIGTERM) == sentinel_sigterm
    assert signal.getsignal(signal.SIGINT) == sentinel_sigint


def test_run_saves_and_restores_a_caller_installed_handler_verbatim(
    tmp_path: Path,
) -> None:
    """Issue #111: `cli.run_composed_pipeline` installs its own persistent
    SIGINT/SIGTERM handler around the whole issue pipeline, once
    `bootstrap_run` hands back a live `IssueOrchestrator`, so that an idle
    window between two provider attempts (e.g. `run_provider_attempts`' own
    backoff sleep) is not left with Python's raw default disposition. That
    handler composes with this module's own -- installed only for the
    duration of one child call -- *without any change here* only because
    `run()` treats whatever was previously installed as fully opaque: it is
    saved verbatim by `signal.signal()`'s own return value and handed back
    to `signal.signal()` unchanged once this call returns, regardless of
    its concrete type. This proves that generic contract explicitly for a
    plain Python function (indistinguishable, as far as this module is
    concerned, from `cli.py`'s own closure), not only for whatever handler
    happened to be installed before the test itself ran."""

    def _caller_handler(signal_number: int, frame: object) -> None:
        del signal_number, frame

    # Capture whatever was actually installed before this test -- pytest's
    # own SIGINT disposition is `signal.default_int_handler`, not `SIG_DFL`
    # -- so the `finally` below restores exactly that, rather than leaking
    # process-wide signal state into later tests in this same process.
    sentinel_sigterm = signal.getsignal(signal.SIGTERM)
    sentinel_sigint = signal.getsignal(signal.SIGINT)

    signal.signal(signal.SIGTERM, _caller_handler)
    signal.signal(signal.SIGINT, _caller_handler)
    try:
        runner = SubprocessRunner(RealClock())
        sink = RecordingAttemptLogSink()
        spec = _spec(_helper_argv(), tmp_path)

        runner.run(spec, sink=sink)

        assert signal.getsignal(signal.SIGTERM) is _caller_handler
        assert signal.getsignal(signal.SIGINT) is _caller_handler
    finally:
        signal.signal(signal.SIGTERM, sentinel_sigterm)
        signal.signal(signal.SIGINT, sentinel_sigint)


def test_run_honors_a_signal_delivered_the_instant_popen_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #112: the SIGTERM/SIGINT handlers must already be installed
    *before* `subprocess.Popen()` is called, not only after the child is
    spawned and its reader/writer threads are started. This sends the
    process its own SIGTERM the instant the real `Popen()` returns --
    simulating one arriving during the spawn syscall itself, the worst case
    of the vulnerable window -- and asserts it is still caught as a
    cancellation and drives the documented escalation. Before the fix, a
    signal delivered this early would hit Python's default disposition
    (SIGTERM) or raise an uncaught `KeyboardInterrupt` (SIGINT) instead,
    which -- among other things -- would abort this very test process
    rather than being reported as `INTERRUPTED`."""

    real_popen = subprocess.Popen

    def _popen_then_self_signal(
        args: tuple[str, ...],
        *,
        cwd: Path,
        stdin: int,
        stdout: int,
        stderr: int,
        env: dict[str, str],
        shell: bool,
        start_new_session: bool,
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            args,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            shell=shell,
            start_new_session=start_new_session,
        )
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr(subprocess, "Popen", _popen_then_self_signal)

    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = ProcessSpec(
        argv=_helper_argv("--sleep", "10"),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=30.0,
        termination_grace_seconds=0.5,
    )

    started = time.monotonic()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert result.outcome is RunOutcome.INTERRUPTED
    assert result.timed_out is False
    assert result.termination_confirmed is True


def test_run_preserves_a_caught_cancellation_when_the_spawn_itself_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #112 (Codex review, P2): if SIGTERM/SIGINT is caught while
    `Popen()` is running but the spawn itself then raises `OSError` (e.g.
    an invalid executable races with shutdown), the cancellation must still
    win -- not be silently swallowed into an ordinary `PROCESS_ERROR`,
    which would hide from the orchestrator that a shutdown was already
    requested."""

    def _self_signal_then_fail(
        *args: object, **kwargs: object
    ) -> subprocess.Popen[bytes]:
        os.kill(os.getpid(), signal.SIGTERM)
        raise OSError("simulated spawn failure racing with shutdown")

    monkeypatch.setattr(subprocess, "Popen", _self_signal_then_fail)

    runner = SubprocessRunner(RealClock())
    sink = RecordingAttemptLogSink()
    spec = _spec(_helper_argv(), tmp_path)

    result = runner.run(spec, sink=sink)

    assert result.outcome is RunOutcome.INTERRUPTED
    assert result.return_code is None
    assert result.termination_confirmed is True
    assert sink.writes == []


def test_run_terminates_the_child_and_reports_logging_error_on_a_sink_fault(
    tmp_path: Path,
) -> None:
    """A sink write fault must stop the child well before its own deadline
    (proving the *fault*, not the timeout, drove the early return),
    terminate it the same bounded way as a timeout, and be reported as
    `LOGGING_ERROR` -- never `PROCESS_ERROR` and never provider-retryable
    (M05-04)."""

    runner = SubprocessRunner(RealClock())
    sink = FaultingAttemptLogSink(fail_after=1)
    spec = ProcessSpec(
        argv=_helper_argv("--large", "500000"),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=10.0,
        termination_grace_seconds=0.5,
    )

    started = time.monotonic()
    result = runner.run(spec, sink=sink)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert result.outcome is RunOutcome.LOGGING_ERROR
    assert result.termination_confirmed is True


def test_run_preserves_partial_output_captured_before_a_sink_fault(
    tmp_path: Path,
) -> None:
    runner = SubprocessRunner(RealClock())
    sink = FaultingAttemptLogSink(fail_after=1)
    total_size = 500_000
    spec = ProcessSpec(
        argv=_helper_argv("--large", str(total_size)),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=10.0,
        termination_grace_seconds=0.5,
    )

    result = runner.run(spec, sink=sink)

    captured = result.stdout_byte_count + result.stderr_byte_count
    assert captured > 0
    assert captured < 2 * total_size
    assert len(sink.writes) == 1

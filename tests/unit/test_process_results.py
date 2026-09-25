"""Unit tests for the pure, deterministic parts of the process boundary.

These tests never spawn a real child; `SubprocessRunner`'s actual
`subprocess.Popen` behavior (spawn failure, non-zero exit, stdin, cwd, and
environment isolation) is covered by `tests/component/test_process_runner.py`
with real local helper processes.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import ProcessResult, RunOutcome
from opencode_tools.process import (
    _wait_for_exit_or_deadline,
    build_process_result,
    deadline_ns,
    sanitize_command,
)


class FakeClock:
    """A `Clock` fake with a fixed wall-clock time and a step counter."""

    def __init__(self, *, start_ns: int = 0) -> None:
        self._monotonic_ns = start_ns

    def now(self) -> datetime:
        return STARTED_AT

    def monotonic_ns(self) -> int:
        self._monotonic_ns += 1
        return self._monotonic_ns


STARTED_AT = datetime(2026, 9, 13, 8, 0, 0, tzinfo=UTC)
FINISHED_AT = datetime(2026, 9, 13, 8, 0, 1, tzinfo=UTC)
COMMAND = ("/usr/bin/git", "status")
CWD = Path("/workspaces/example/backend")
LOG_PATH = Path("architect-provider-attempt-1.log")


def _empty_digest() -> str:
    return hashlib.sha256(b"").hexdigest()


def _build(
    *,
    return_code: int | None,
    termination_confirmed: bool = True,
    timed_out: bool = False,
    interrupted: bool = False,
    logging_error: bool = False,
    stdout_byte_count: int = 0,
    stdout_sha256: str | None = None,
    stderr_byte_count: int = 0,
    stderr_sha256: str | None = None,
    duration_ns: int = 1_000_000,
) -> ProcessResult:
    return build_process_result(
        command=COMMAND,
        cwd=CWD,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ns=duration_ns,
        return_code=return_code,
        termination_confirmed=termination_confirmed,
        timed_out=timed_out,
        interrupted=interrupted,
        logging_error=logging_error,
        stdout_byte_count=stdout_byte_count,
        stdout_sha256=stdout_sha256 or _empty_digest(),
        stderr_byte_count=stderr_byte_count,
        stderr_sha256=stderr_sha256 or _empty_digest(),
        log_path=LOG_PATH,
    )


def test_build_process_result_maps_a_zero_return_code_to_succeeded() -> None:
    result = _build(return_code=0)

    assert isinstance(result, ProcessResult)
    assert result.outcome is RunOutcome.SUCCEEDED
    assert result.return_code == 0
    assert result.timed_out is False
    assert result.termination_confirmed is True


def test_build_process_result_maps_a_nonzero_return_code_to_process_error() -> None:
    result = _build(return_code=17, stderr_sha256=hashlib.sha256(b"boom\n").hexdigest())

    assert result.outcome is RunOutcome.PROCESS_ERROR
    assert result.return_code == 17


def test_build_process_result_maps_a_none_return_code_to_process_error() -> None:
    """A `None` return code (a spawn that never produced a child) is a
    `PROCESS_ERROR`, never a retryable provider outcome (AC-015)."""

    result = _build(return_code=None)

    assert result.return_code is None
    assert result.outcome is RunOutcome.PROCESS_ERROR
    assert result.termination_confirmed is True


def test_build_process_result_preserves_a_false_termination_confirmed() -> None:
    """A reader thread still stuck on a lingering descendant after the join
    bound elapses (System Design SS10.2) must surface as an unconfirmed,
    not a confirmed, termination -- this function must not silently upgrade
    it to `True`."""

    result = _build(return_code=0, termination_confirmed=False)

    assert result.termination_confirmed is False


def test_build_process_result_maps_a_timeout_to_timeout_regardless_of_return_code() -> (
    None
):
    """A killed-by-signal return code (negative on POSIX) must not slip
    into `PROCESS_ERROR`: a deadline miss is always `TIMEOUT` (System
    Design SS10.2), and `TIMEOUT` is never provider-retryable (AC-015's
    sibling guarantee for timeouts, ADR-004)."""

    result = _build(return_code=-15, termination_confirmed=True, timed_out=True)

    assert result.outcome is RunOutcome.TIMEOUT
    assert result.timed_out is True


def test_build_process_result_maps_interrupted_to_interrupted() -> None:
    result = _build(return_code=-15, termination_confirmed=True, interrupted=True)

    assert result.outcome is RunOutcome.INTERRUPTED
    assert result.timed_out is False


def test_build_process_result_maps_logging_error_to_logging_error() -> None:
    """A sink write fault is `LOGGING_ERROR`, not `PROCESS_ERROR`: process
    and logging failures must stay distinct categories (M05-04), and
    neither is ever provider-retryable."""

    result = _build(return_code=-15, termination_confirmed=True, logging_error=True)

    assert result.outcome is RunOutcome.LOGGING_ERROR
    assert result.timed_out is False


def test_build_process_result_rejects_timed_out_and_interrupted_together() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _build(return_code=None, timed_out=True, interrupted=True)


def test_build_process_result_rejects_any_two_of_the_three_stop_reasons_together() -> (
    None
):
    with pytest.raises(ValueError, match="mutually exclusive"):
        _build(return_code=None, interrupted=True, logging_error=True)
    with pytest.raises(ValueError, match="mutually exclusive"):
        _build(return_code=None, timed_out=True, logging_error=True)


def test_build_process_result_timeout_takes_precedence_when_termination_unconfirmed() -> (
    None
):
    """An unconfirmed termination during a timeout escalation is still a
    `TIMEOUT`, not silently downgraded to a generic `PROCESS_ERROR` -- the
    distinction matters for diagnosing a stuck deadline versus a plain
    technical failure."""

    result = _build(return_code=None, termination_confirmed=False, timed_out=True)

    assert result.outcome is RunOutcome.TIMEOUT
    assert result.termination_confirmed is False


def test_build_process_result_preserves_precomputed_byte_count_and_sha256() -> None:
    stdout_digest = hashlib.sha256(b"hello stdout\n").hexdigest()
    stderr_digest = hashlib.sha256(b"hello stderr\n").hexdigest()

    result = _build(
        return_code=0,
        stdout_byte_count=13,
        stdout_sha256=stdout_digest,
        stderr_byte_count=13,
        stderr_sha256=stderr_digest,
    )

    assert result.stdout_byte_count == 13
    assert result.stdout_sha256 == stdout_digest
    assert result.stderr_byte_count == 13
    assert result.stderr_sha256 == stderr_digest


def test_build_process_result_does_not_compute_digests_itself() -> None:
    """This function assembles a result from already-computed facts; it
    must never hash or count bytes on its own (that happens incrementally
    in `_StreamReader`, System Design SS10.1), so a mismatched digest passed
    in is preserved verbatim rather than silently corrected."""

    result = _build(
        return_code=0, stdout_byte_count=999, stdout_sha256="not-a-real-digest"
    )

    assert result.stdout_byte_count == 999
    assert result.stdout_sha256 == "not-a-real-digest"


def test_build_process_result_uses_clock_supplied_timestamps_and_duration() -> None:
    """Timing comes entirely from the caller's clock facts, never from
    `datetime.now()` or `time.monotonic()` called inside this function."""

    result = _build(return_code=0, duration_ns=42)

    assert result.started_at == STARTED_AT
    assert result.finished_at == FINISHED_AT
    assert result.duration_ns == 42


def test_build_process_result_echoes_the_sinks_log_path() -> None:
    result = _build(return_code=0)

    assert result.log_path == LOG_PATH


def test_build_process_result_preserves_the_sanitized_command_tuple() -> None:
    result = _build(return_code=0)

    assert result.command == COMMAND
    assert result.cwd == CWD


def test_deadline_ns_adds_timeout_seconds_converted_to_nanoseconds() -> None:
    assert deadline_ns(1_000, 2.0) == 1_000 + 2_000_000_000


def test_deadline_ns_truncates_fractional_nanoseconds() -> None:
    # 0.1s == 100_000_000ns exactly, but a value that doesn't divide evenly
    # (e.g. thirds of a second) must still return an int deadline.
    result = deadline_ns(0, 1 / 3)

    assert isinstance(result, int)
    assert result == int((1 / 3) * 1_000_000_000)


def test_deadline_ns_is_a_pure_function_of_a_fake_clocks_readings() -> None:
    """`deadline_ns` needs no real process and no real waiting: given a
    fake clock's own start reading, it is entirely deterministic (M05-03's
    "deadline con clock fake" test requirement)."""

    clock = FakeClock(start_ns=0)
    start = clock.monotonic_ns()
    deadline = deadline_ns(start, timeout_seconds=0.000001)

    assert clock.monotonic_ns() < deadline  # one tick later, still short of it
    for _ in range(2_000):
        clock.monotonic_ns()
    assert clock.monotonic_ns() >= deadline  # many ticks later, past it


def test_sanitize_command_returns_argv_unchanged_when_no_credentials_present() -> None:
    argv = ("/usr/bin/git", "status", "--porcelain")

    assert sanitize_command(argv) == argv


def test_sanitize_command_redacts_credentials_embedded_in_a_url() -> None:
    argv = (
        "/usr/bin/git",
        "clone",
        "https://x-access-token:secret-token@github.com/example/repo.git",
    )

    sanitized = sanitize_command(argv)

    assert sanitized[:2] == argv[:2]
    assert "secret-token" not in sanitized[2]
    assert "x-access-token" not in sanitized[2]
    assert sanitized[2] == "https://REDACTED@github.com/example/repo.git"


def test_sanitize_command_redacts_every_credential_bearing_argument() -> None:
    argv = (
        "/usr/bin/curl",
        "https://user:pw@example.com/a",
        "https://other:pw2@example.org/b",
    )

    sanitized = sanitize_command(argv)

    assert "pw" not in sanitized[1]
    assert "pw2" not in sanitized[2]


def test_sanitize_command_never_alters_argument_count_or_order() -> None:
    argv = ("/bin/echo", "one", "two", "three")

    assert len(sanitize_command(argv)) == len(argv)
    assert sanitize_command(argv)[1:] == argv[1:]


def test_sanitize_command_returns_a_tuple_not_the_original_list_type() -> None:
    argv = ("/bin/echo", "hello")

    result = sanitize_command(argv)

    assert isinstance(result, tuple)


def test_sanitize_command_rejects_non_tuple_argv() -> None:
    with pytest.raises(TypeError, match="argv must be a tuple of strings"):
        sanitize_command(["/bin/echo", "hello"])  # type: ignore[arg-type]


def test_sanitize_command_rejects_non_string_items() -> None:
    with pytest.raises(TypeError, match="argv must be a tuple of strings"):
        sanitize_command(("/bin/echo", 1))  # type: ignore[arg-type]


def test_sanitize_command_leaves_plain_filesystem_paths_untouched() -> None:
    """`argv` cannot legally carry the prompt (`ProcessSpec` routes it via
    stdin only); this documents that the sanitizer leaves ordinary string
    content, e.g. a filesystem path, untouched."""

    argv = ("/usr/bin/git", "-C", str(Path("/workspaces/example/backend")))

    assert sanitize_command(argv) == argv


class _FakeExitedProcess:
    """A minimal `Popen`-shaped fake that reports as already exited."""

    def __init__(self, return_code: int) -> None:
        self._return_code = return_code

    def poll(self) -> int | None:
        return self._return_code


def test_wait_for_exit_or_deadline_lets_a_cancellation_outrank_an_already_exited_child() -> (
    None
):
    """A caught SIGINT/SIGTERM must win the same race `sink_fault` already
    wins against `process.poll()` (see the function's own docstring):
    otherwise a short-lived child that happens to finish at the same moment
    a signal is caught would silently report its own exit code instead of
    `INTERRUPTED`, breaking the documented "always" contract
    (`build_process_result`'s own docstring, SH-001, issue #112)."""

    cancelled = threading.Event()
    cancelled.set()
    process = _FakeExitedProcess(return_code=0)

    return_code, timed_out, interrupted, logging_error = _wait_for_exit_or_deadline(
        process,  # type: ignore[arg-type]
        FakeClock(),
        deadline_ns(0, 30.0),
        cancelled,
        threading.Event(),
    )

    assert interrupted is True
    assert timed_out is False
    assert logging_error is False
    assert return_code is None


def test_wait_for_exit_or_deadline_reports_an_uncancelled_exit_normally() -> None:
    """The reordering that lets cancellation outrank `process.poll()` must
    not change the ordinary, uncancelled path: an already-exited child is
    still reported with its real return code."""

    process = _FakeExitedProcess(return_code=0)

    return_code, timed_out, interrupted, logging_error = _wait_for_exit_or_deadline(
        process,  # type: ignore[arg-type]
        FakeClock(),
        deadline_ns(0, 30.0),
        threading.Event(),
        threading.Event(),
    )

    assert return_code == 0
    assert timed_out is False
    assert interrupted is False
    assert logging_error is False

"""Unit tests for the pure, deterministic parts of the process boundary.

These tests never spawn a real child; `SubprocessRunner`'s actual
`subprocess.Popen` behavior (spawn failure, non-zero exit, stdin, cwd, and
environment isolation) is covered by `tests/component/test_process_runner.py`
with real local helper processes.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import ProcessResult, RunOutcome
from opencode_tools.process import build_process_result, sanitize_command

STARTED_AT = datetime(2026, 9, 13, 8, 0, 0, tzinfo=UTC)
FINISHED_AT = datetime(2026, 9, 13, 8, 0, 1, tzinfo=UTC)
COMMAND = ("/usr/bin/git", "status")
CWD = Path("/workspaces/example/backend")
LOG_PATH = Path("architect-provider-attempt-1.log")


def _empty_digest() -> str:
    return hashlib.sha256(b"").hexdigest()


def test_build_process_result_maps_a_zero_return_code_to_succeeded() -> None:
    result = build_process_result(
        command=COMMAND,
        cwd=CWD,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ns=1_000_000,
        return_code=0,
        stdout=b"",
        stderr=b"",
        log_path=LOG_PATH,
    )

    assert isinstance(result, ProcessResult)
    assert result.outcome is RunOutcome.SUCCEEDED
    assert result.return_code == 0
    assert result.timed_out is False
    assert result.termination_confirmed is True


def test_build_process_result_maps_a_nonzero_return_code_to_process_error() -> None:
    result = build_process_result(
        command=COMMAND,
        cwd=CWD,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ns=1_000_000,
        return_code=17,
        stdout=b"",
        stderr=b"boom\n",
        log_path=LOG_PATH,
    )

    assert result.outcome is RunOutcome.PROCESS_ERROR
    assert result.return_code == 17


def test_build_process_result_maps_a_none_return_code_to_process_error() -> None:
    """A `None` return code (a spawn that never produced a child) is a
    `PROCESS_ERROR`, never a retryable provider outcome (AC-015)."""

    result = build_process_result(
        command=COMMAND,
        cwd=CWD,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ns=1_000_000,
        return_code=None,
        stdout=b"",
        stderr=b"",
        log_path=LOG_PATH,
    )

    assert result.return_code is None
    assert result.outcome is RunOutcome.PROCESS_ERROR
    assert result.termination_confirmed is True


def test_build_process_result_computes_byte_count_and_sha256_per_stream() -> None:
    stdout = b"hello stdout\n"
    stderr = b"hello stderr\n"

    result = build_process_result(
        command=COMMAND,
        cwd=CWD,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ns=1_000_000,
        return_code=0,
        stdout=stdout,
        stderr=stderr,
        log_path=LOG_PATH,
    )

    assert result.stdout_byte_count == len(stdout)
    assert result.stdout_sha256 == hashlib.sha256(stdout).hexdigest()
    assert result.stderr_byte_count == len(stderr)
    assert result.stderr_sha256 == hashlib.sha256(stderr).hexdigest()


def test_build_process_result_digests_empty_streams_as_the_empty_sha256() -> None:
    result = build_process_result(
        command=COMMAND,
        cwd=CWD,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ns=1_000_000,
        return_code=0,
        stdout=b"",
        stderr=b"",
        log_path=LOG_PATH,
    )

    assert result.stdout_byte_count == 0
    assert result.stdout_sha256 == _empty_digest()
    assert result.stderr_byte_count == 0
    assert result.stderr_sha256 == _empty_digest()


def test_build_process_result_uses_clock_supplied_timestamps_and_duration() -> None:
    """Timing comes entirely from the caller's clock facts, never from
    `datetime.now()` or `time.monotonic()` called inside this function."""

    result = build_process_result(
        command=COMMAND,
        cwd=CWD,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ns=42,
        return_code=0,
        stdout=b"",
        stderr=b"",
        log_path=LOG_PATH,
    )

    assert result.started_at == STARTED_AT
    assert result.finished_at == FINISHED_AT
    assert result.duration_ns == 42


def test_build_process_result_echoes_the_sinks_log_path() -> None:
    result = build_process_result(
        command=COMMAND,
        cwd=CWD,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ns=1,
        return_code=0,
        stdout=b"",
        stderr=b"",
        log_path=LOG_PATH,
    )

    assert result.log_path == LOG_PATH


def test_build_process_result_preserves_the_sanitized_command_tuple() -> None:
    result = build_process_result(
        command=COMMAND,
        cwd=CWD,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ns=1,
        return_code=0,
        stdout=b"",
        stderr=b"",
        log_path=LOG_PATH,
    )

    assert result.command == COMMAND
    assert result.cwd == CWD


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

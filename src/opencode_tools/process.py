"""Generic, bounded POSIX child-process boundary (System Design SS10.1).

`SubprocessRunner` spawns exactly one child via `subprocess.Popen` with a
structured argv and `shell=False`; it knows nothing about OpenCode, Git,
GitHub, the agent protocol, or provider classification, and it never calls
`os.chdir()`. Timeout enforcement, process-group termination, concurrent
bounded draining, and sink-fault handling are later milestones; this module
only covers the spawn/capture/result boundary (System Design SS10.1).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from opencode_tools.domain import ProcessResult, ProcessSpec, RunOutcome
from opencode_tools.ports import AttemptLogSink, Clock

_CREDENTIAL_IN_URL = re.compile(r"://[^/@\s]+@")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def sanitize_command(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Return `argv` with any URL-embedded credential replaced by a marker.

    The prompt can never reach `argv`: `ProcessSpec` routes it through stdin
    only. This only strips incidental credentials, such as a token embedded
    in a Git remote URL, before the command is recorded (System Design
    SS18.3).
    """

    if not isinstance(argv, tuple) or not all(isinstance(item, str) for item in argv):
        raise TypeError("argv must be a tuple of strings")
    return tuple(_CREDENTIAL_IN_URL.sub("://REDACTED@", item) for item in argv)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest() if payload else _EMPTY_SHA256


def build_process_result(
    *,
    command: tuple[str, ...],
    cwd: Path,
    started_at: datetime,
    finished_at: datetime,
    duration_ns: int,
    return_code: int | None,
    stdout: bytes,
    stderr: bytes,
    log_path: Path,
) -> ProcessResult:
    """Assemble the technical `ProcessResult` for one completed invocation.

    This is the pure half of the process boundary: given the raw facts of a
    spawn (or spawn failure), it computes byte counts, SHA-256 digests, and
    the outcome -- `SUCCEEDED` for a zero return code, `PROCESS_ERROR`
    otherwise, including a `None` return code from a spawn failure (AC-015)
    -- without touching `subprocess` or the filesystem. Timeout enforcement
    and unconfirmed termination are later milestones (System Design SS10.2);
    every result built here is `timed_out=False` with
    `termination_confirmed=True`.
    """

    outcome = RunOutcome.SUCCEEDED if return_code == 0 else RunOutcome.PROCESS_ERROR
    return ProcessResult(
        command=command,
        cwd=cwd,
        started_at=started_at,
        finished_at=finished_at,
        duration_ns=duration_ns,
        return_code=return_code,
        timed_out=False,
        termination_confirmed=True,
        log_path=log_path,
        stdout_byte_count=len(stdout),
        stdout_sha256=_digest(stdout),
        stderr_byte_count=len(stderr),
        stderr_sha256=_digest(stderr),
        outcome=outcome,
    )


def _encode_stdin(stdin: str | bytes | None) -> bytes | None:
    if stdin is None:
        return None
    if isinstance(stdin, bytes):
        return stdin
    return stdin.encode("utf-8")


class SubprocessRunner:
    """The `ProcessRunner` adapter backed by `subprocess.Popen`."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def run(self, spec: ProcessSpec, *, sink: AttemptLogSink) -> ProcessResult:
        """Run `spec` to completion and return its technical `ProcessResult`.

        The child's cwd is set per-invocation; the parent never calls
        `os.chdir()`. The environment is inherited from the current process
        and merged with `spec.environment_overrides`, but it is never
        enumerated or persisted into the result. Bounding by
        `spec.timeout_seconds` is not yet applied here (System Design SS10.2
        lands in a later milestone).
        """

        if type(spec) is not ProcessSpec:
            raise TypeError("spec must be ProcessSpec")

        command = sanitize_command(spec.argv)
        environment = {**os.environ, **spec.environment_overrides}
        stdin_payload = _encode_stdin(spec.stdin)

        started_at = self._clock.now()
        start_ns = self._clock.monotonic_ns()

        try:
            process = subprocess.Popen(
                spec.argv,
                cwd=spec.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
            )
        except OSError:
            finished_at = self._clock.now()
            duration_ns = self._clock.monotonic_ns() - start_ns
            return build_process_result(
                command=command,
                cwd=spec.cwd,
                started_at=started_at,
                finished_at=finished_at,
                duration_ns=duration_ns,
                return_code=None,
                stdout=b"",
                stderr=b"",
                log_path=sink.path,
            )

        stdout_bytes, stderr_bytes = process.communicate(input=stdin_payload)
        finished_at = self._clock.now()
        duration_ns = self._clock.monotonic_ns() - start_ns

        if stdout_bytes:
            sink.write("stdout", stdout_bytes, finished_at)
        if stderr_bytes:
            sink.write("stderr", stderr_bytes, finished_at)

        return build_process_result(
            command=command,
            cwd=spec.cwd,
            started_at=started_at,
            finished_at=finished_at,
            duration_ns=duration_ns,
            return_code=process.returncode,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            log_path=sink.path,
        )


__all__ = ("SubprocessRunner", "build_process_result", "sanitize_command")

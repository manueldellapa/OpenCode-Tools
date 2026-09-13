"""Generic, bounded POSIX child-process boundary (System Design SS10.1).

`SubprocessRunner` spawns exactly one child via `subprocess.Popen` with a
structured argv and `shell=False`; it knows nothing about OpenCode, Git,
GitHub, the agent protocol, or provider classification, and it never calls
`os.chdir()`. stdout/stderr are drained concurrently in bounded chunks and
forwarded incrementally to the sink, never accumulated whole in memory.
Deadline enforcement and process-group termination (System Design SS10.2)
are a later milestone; this module only covers the spawn/drain/result
boundary.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import IO, cast

from opencode_tools.domain import ProcessResult, ProcessSpec, RunOutcome
from opencode_tools.ports import AttemptLogSink, Clock, LogChannel

_CREDENTIAL_IN_URL = re.compile(r"://[^/@\s]+@")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_CHUNK_SIZE = 65536


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


def build_process_result(
    *,
    command: tuple[str, ...],
    cwd: Path,
    started_at: datetime,
    finished_at: datetime,
    duration_ns: int,
    return_code: int | None,
    termination_confirmed: bool,
    stdout_byte_count: int,
    stdout_sha256: str,
    stderr_byte_count: int,
    stderr_sha256: str,
    log_path: Path,
) -> ProcessResult:
    """Assemble the technical `ProcessResult` for one completed invocation.

    This is the pure half of the process boundary: given the raw facts of a
    spawn (or spawn failure) -- already-computed byte counts and SHA-256
    digests, since draining happens incrementally in `SubprocessRunner`, not
    from one accumulated blob here -- it decides the outcome. `SUCCEEDED`
    requires both a zero return code *and* confirmed termination (the domain
    invariant); everything else, including a `None` return code from a
    spawn failure (AC-015) or a return code of 0 whose reader threads never
    confirmed a clean join, is `PROCESS_ERROR`. This never touches
    `subprocess` or the filesystem. Deadline-based timeout is a later
    milestone (System Design SS10.2); every result built here is
    `timed_out=False`.
    """

    outcome = (
        RunOutcome.SUCCEEDED
        if return_code == 0 and termination_confirmed
        else RunOutcome.PROCESS_ERROR
    )
    return ProcessResult(
        command=command,
        cwd=cwd,
        started_at=started_at,
        finished_at=finished_at,
        duration_ns=duration_ns,
        return_code=return_code,
        timed_out=False,
        termination_confirmed=termination_confirmed,
        log_path=log_path,
        stdout_byte_count=stdout_byte_count,
        stdout_sha256=stdout_sha256,
        stderr_byte_count=stderr_byte_count,
        stderr_sha256=stderr_sha256,
        outcome=outcome,
    )


def _encode_stdin(stdin: str | bytes | None) -> bytes | None:
    if stdin is None:
        return None
    if isinstance(stdin, bytes):
        return stdin
    return stdin.encode("utf-8")


class _StdinWriter(threading.Thread):
    """Writes the prompt/stdin payload on its own thread, then closes it.

    A dedicated thread for stdin (mirrored by one reader thread per output
    stream) is what lets a child block on a full stdout/stderr pipe while
    still being written to, and vice versa, without either side deadlocking
    the other (System Design SS10.1).
    """

    def __init__(self, *, stream: IO[bytes], payload: bytes | None) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._payload = payload

    def run(self) -> None:
        try:
            if self._payload:
                self._stream.write(self._payload)
                self._stream.flush()
        except OSError:
            pass
        finally:
            try:
                self._stream.close()
            except OSError:
                pass


class _StreamReader(threading.Thread):
    """Drains one child stream in bounded chunks on its own thread.

    Each chunk is folded into a running byte count and SHA-256 digest and
    forwarded to `sink` immediately (forwarding incremental, System Design
    SS10.1/SS20.3), so memory stays bounded regardless of how much output
    the child produces. `read1()`, not `read()`, is deliberate: it returns
    as soon as any data is available instead of blocking until a full chunk
    or EOF, which is what makes the forwarding genuinely incremental rather
    than a large buffered read in disguise.
    """

    def __init__(
        self,
        *,
        channel: LogChannel,
        stream: io.BufferedReader,
        sink: AttemptLogSink,
        sink_lock: threading.Lock,
        clock: Clock,
    ) -> None:
        super().__init__(daemon=True)
        self._channel = channel
        self._stream = stream
        self._sink = sink
        self._sink_lock = sink_lock
        self._clock = clock
        self._state_lock = threading.Lock()
        self._byte_count = 0
        self._digest = hashlib.sha256()

    def run(self) -> None:
        try:
            while True:
                try:
                    chunk = self._stream.read1(_CHUNK_SIZE)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                timestamp = self._clock.now()
                with self._state_lock:
                    self._byte_count += len(chunk)
                    self._digest.update(chunk)
                with self._sink_lock:
                    self._sink.write(self._channel, chunk, timestamp)
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def snapshot(self) -> tuple[int, str]:
        """Return the byte count and hex digest captured so far.

        Safe to call even while the thread is still running (e.g. a reader
        stuck on a lingering descendant, System Design SS10.2): it returns
        whatever partial output has been captured, never blocks.
        """

        with self._state_lock:
            return self._byte_count, self._digest.hexdigest()


class SubprocessRunner:
    """The `ProcessRunner` adapter backed by `subprocess.Popen`."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def run(self, spec: ProcessSpec, *, sink: AttemptLogSink) -> ProcessResult:
        """Run `spec` to completion and return its technical `ProcessResult`.

        The child's cwd is set per-invocation; the parent never calls
        `os.chdir()`. The environment is inherited from the current process
        and merged with `spec.environment_overrides`, but it is never
        enumerated or persisted into the result. stdout and stderr are
        drained concurrently and bounded; joining those reader threads is
        itself bounded by `spec.termination_grace_seconds`, so a lingering
        descendant that still holds a pipe open cannot prevent `run()` from
        returning -- it only prevents `termination_confirmed` from being
        `True`. Deadline enforcement on the child itself (killing it after
        `spec.timeout_seconds`) is a later milestone (System Design SS10.2).
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
                termination_confirmed=True,
                stdout_byte_count=0,
                stdout_sha256=_EMPTY_SHA256,
                stderr_byte_count=0,
                stderr_sha256=_EMPTY_SHA256,
                log_path=sink.path,
            )

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        # subprocess.Popen's stdout/stderr are typed IO[bytes] generically,
        # but with the default bufsize and no text/encoding mode they are
        # always io.BufferedReader at runtime, which is what read1() needs.
        stdout_stream = cast(io.BufferedReader, process.stdout)
        stderr_stream = cast(io.BufferedReader, process.stderr)

        sink_lock = threading.Lock()
        stdin_writer = _StdinWriter(stream=process.stdin, payload=stdin_payload)
        stdout_reader = _StreamReader(
            channel="stdout",
            stream=stdout_stream,
            sink=sink,
            sink_lock=sink_lock,
            clock=self._clock,
        )
        stderr_reader = _StreamReader(
            channel="stderr",
            stream=stderr_stream,
            sink=sink,
            sink_lock=sink_lock,
            clock=self._clock,
        )

        stdin_writer.start()
        stdout_reader.start()
        stderr_reader.start()

        return_code = process.wait()

        join_bound = spec.termination_grace_seconds
        stdin_writer.join(timeout=join_bound)
        stdout_reader.join(timeout=join_bound)
        stderr_reader.join(timeout=join_bound)
        termination_confirmed = not (
            stdin_writer.is_alive()
            or stdout_reader.is_alive()
            or stderr_reader.is_alive()
        )

        finished_at = self._clock.now()
        duration_ns = self._clock.monotonic_ns() - start_ns

        stdout_byte_count, stdout_sha256 = stdout_reader.snapshot()
        stderr_byte_count, stderr_sha256 = stderr_reader.snapshot()

        return build_process_result(
            command=command,
            cwd=spec.cwd,
            started_at=started_at,
            finished_at=finished_at,
            duration_ns=duration_ns,
            return_code=return_code,
            termination_confirmed=termination_confirmed,
            stdout_byte_count=stdout_byte_count,
            stdout_sha256=stdout_sha256,
            stderr_byte_count=stderr_byte_count,
            stderr_sha256=stderr_sha256,
            log_path=sink.path,
        )


__all__ = ("SubprocessRunner", "build_process_result", "sanitize_command")

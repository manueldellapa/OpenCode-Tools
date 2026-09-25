"""Generic, bounded POSIX child-process boundary (System Design SS10.1).

`SubprocessRunner` spawns exactly one child via `subprocess.Popen` with a
structured argv and `shell=False`; it knows nothing about OpenCode, Git,
GitHub, the agent protocol, or provider classification, and it never calls
`os.chdir()`. stdout/stderr are drained concurrently in bounded chunks and
forwarded incrementally to the sink, never accumulated whole in memory.
Every child gets its own POSIX session/process group (`start_new_session`);
a deadline miss or an incoming SIGINT/SIGTERM applies the same bounded
`SIGTERM -> grace -> SIGKILL -> grace` escalation to that whole group
(System Design SS10.2), and a lingering, unreachable descendant surfaces as
`termination_confirmed=False` rather than an unbounded wait.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import IO, cast

from opencode_tools.domain import ProcessResult, ProcessSpec, RunOutcome
from opencode_tools.ports import AttemptLogSink, Clock, LogChannel

_CREDENTIAL_IN_URL = re.compile(r"://[^/@\s]+@")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_CHUNK_SIZE = 65536
_POLL_INTERVAL_SECONDS = 0.01


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
    timed_out: bool = False,
    interrupted: bool = False,
    logging_error: bool = False,
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
    from one accumulated blob here -- it decides the outcome, in this order:

    - `timed_out=True` -> `TIMEOUT` (the deadline was missed, System Design
      SS10.2), always, regardless of the return code.
    - `interrupted=True` -> `INTERRUPTED` (a SIGINT/SIGTERM cancellation,
      SH-001), always.
    - `logging_error=True` -> `LOGGING_ERROR` (a sink write faulted, e.g.
      disk full or permission denied; the child was already terminated,
      M05-04), always.
    - otherwise `SUCCEEDED` requires both a zero return code *and*
      confirmed termination (the domain invariant); everything else,
      including a `None` return code from a spawn failure (AC-015) or a
      return code of 0 whose reader threads never confirmed a clean join,
      is `PROCESS_ERROR`.

    Exactly zero or one of `timed_out`/`interrupted`/`logging_error` may be
    `True`. This never touches `subprocess` or the filesystem, and never
    retries -- a caller mapping any of these three into a provider retry
    would be a policy bug elsewhere, not something this function permits.
    """

    if sum((timed_out, interrupted, logging_error)) > 1:
        raise ValueError(
            "timed_out, interrupted, and logging_error are mutually exclusive"
        )

    if timed_out:
        outcome = RunOutcome.TIMEOUT
    elif interrupted:
        outcome = RunOutcome.INTERRUPTED
    elif logging_error:
        outcome = RunOutcome.LOGGING_ERROR
    elif return_code == 0 and termination_confirmed:
        outcome = RunOutcome.SUCCEEDED
    else:
        outcome = RunOutcome.PROCESS_ERROR

    return ProcessResult(
        command=command,
        cwd=cwd,
        started_at=started_at,
        finished_at=finished_at,
        duration_ns=duration_ns,
        return_code=return_code,
        timed_out=timed_out,
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


def deadline_ns(start_ns: int, timeout_seconds: float) -> int:
    """Return the monotonic-clock deadline for a spawn that started at
    `start_ns` and is bounded by `timeout_seconds` (System Design SS10.2).

    A pure function of already-known facts, deliberately kept separate from
    `SubprocessRunner` so the deadline arithmetic itself is unit-testable
    with a fake clock, without spawning any real process.
    """

    return start_ns + int(timeout_seconds * 1_000_000_000)


def _signal_process_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_for_exit_or_deadline(
    process: subprocess.Popen[bytes],
    clock: Clock,
    deadline: int,
    cancelled: threading.Event,
    sink_fault: threading.Event,
) -> tuple[int | None, bool, bool, bool]:
    """Poll until the child exits, the deadline passes, cancellation is
    requested, or a reader's sink write faults; never blocks past whichever
    comes first.

    Returns `(return_code, timed_out, interrupted, logging_error)`:
    `return_code` is set only when the child had already exited on its own
    with neither a fault nor a cancellation observed. A sink fault and a
    cancellation are both checked *before* the child's own exit status: a
    faulted reader keeps draining (rather than closing its pipe) precisely
    so the child is not incidentally broken-piped into its own unrelated
    exit code while we are still noticing the fault (M05-04), and a
    SIGINT/SIGTERM already caught (`cancelled` set, issue #112) must win
    the same way -- otherwise a short-lived child that happens to finish at
    the same moment would silently report its own exit code instead of
    `INTERRUPTED`, swallowing the shutdown request the `SIGTERM -> grace ->
    SIGKILL -> grace` escalation (SH-001) is supposed to always trigger.
    """

    while True:
        if sink_fault.is_set():
            return None, False, False, True
        if cancelled.is_set():
            return None, False, True, False
        return_code = process.poll()
        if return_code is not None:
            return return_code, False, False, False
        if clock.monotonic_ns() >= deadline:
            return None, True, False, False
        time.sleep(_POLL_INTERVAL_SECONDS)


def _escalate_and_confirm_termination(
    process: subprocess.Popen[bytes],
    grace_seconds: float,
) -> tuple[int | None, bool]:
    """Apply `SIGTERM -> grace -> SIGKILL -> grace` to the child's whole
    process group (System Design SS10.2) and report whether termination was
    confirmed within that bound.

    `start_new_session=True` at spawn time makes the child both its session
    and process-group leader, so signaling `process.pid` via `killpg`
    reaches it and every descendant still in that group -- but not one that
    escaped it (e.g. by calling `os.setsid()` itself), which is exactly the
    case this can legitimately fail to confirm.
    """

    pid = process.pid

    _signal_process_group(pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
        return process.returncode, True
    except subprocess.TimeoutExpired:
        pass

    _signal_process_group(pid, signal.SIGKILL)
    try:
        process.wait(timeout=grace_seconds)
        return process.returncode, True
    except subprocess.TimeoutExpired:
        return process.poll(), False


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

    A `sink.write()` failure (`OSError`, e.g. disk full or permission
    denied) sets `sink_fault` and stops counting/forwarding, but keeps
    draining (and discarding) the pipe rather than closing it: closing our
    end immediately would deliver a broken pipe to a child that is still
    actively writing, letting it crash on its own with an unrelated
    technical exit code -- a race that would non-deterministically report
    `PROCESS_ERROR` instead of `LOGGING_ERROR` depending on who notices
    first. `SubprocessRunner` is the one that actually terminates the child
    once it observes `sink_fault` set (M05-04).
    """

    def __init__(
        self,
        *,
        channel: LogChannel,
        stream: io.BufferedReader,
        sink: AttemptLogSink,
        sink_lock: threading.Lock,
        sink_fault: threading.Event,
        clock: Clock,
    ) -> None:
        super().__init__(daemon=True)
        self._channel = channel
        self._stream = stream
        self._sink = sink
        self._sink_lock = sink_lock
        self._sink_fault = sink_fault
        self._clock = clock
        self._state_lock = threading.Lock()
        self._byte_count = 0
        self._digest = hashlib.sha256()

    def run(self) -> None:
        faulted = False
        try:
            while True:
                try:
                    chunk = self._stream.read1(_CHUNK_SIZE)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                if faulted:
                    # Keep draining to avoid backing the child into a
                    # broken pipe; there is nothing left to do with the
                    # data once the sink itself has failed.
                    continue
                timestamp = self._clock.now()
                with self._state_lock:
                    self._byte_count += len(chunk)
                    self._digest.update(chunk)
                try:
                    with self._sink_lock:
                        self._sink.write(self._channel, chunk, timestamp)
                except OSError:
                    self._sink_fault.set()
                    faulted = True
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
        drained concurrently and bounded.

        The child runs in its own POSIX session/process group. If it has
        not exited by `spec.timeout_seconds`, or if this process receives
        SIGINT/SIGTERM while it is running (SH-001), the same bounded
        `SIGTERM -> grace -> SIGKILL -> grace` escalation is applied to that
        whole group (System Design SS10.2). The SIGTERM/SIGINT handlers are
        installed *before* `subprocess.Popen()` is even called, not after
        the child is spawned and its reader/writer threads are started: a
        signal arriving anywhere from the spawn syscall onward is always
        caught as a cancellation and drives the same escalation, rather than
        risking Python's default disposition (an unhandled `SIGTERM`) or an
        uncaught `KeyboardInterrupt` unwinding past `run()` and leaving the
        already-spawned child unsupervised (issue #112). Joining the reader
        threads afterward is itself bounded by `spec.termination_grace_seconds`,
        so a descendant that escaped the group (e.g. via its own `setsid()`)
        and keeps a pipe open can never prevent `run()` from returning --
        it only prevents `termination_confirmed` from being `True`.
        """

        if type(spec) is not ProcessSpec:
            raise TypeError("spec must be ProcessSpec")

        command = sanitize_command(spec.argv)
        environment = {**os.environ, **spec.environment_overrides}
        stdin_payload = _encode_stdin(spec.stdin)

        started_at = self._clock.now()
        start_ns = self._clock.monotonic_ns()

        cancelled = threading.Event()

        def _request_cancellation(signal_number: int, frame: FrameType | None) -> None:
            del signal_number, frame
            cancelled.set()

        previous_sigterm = signal.signal(signal.SIGTERM, _request_cancellation)
        previous_sigint = signal.signal(signal.SIGINT, _request_cancellation)
        try:
            try:
                process = subprocess.Popen(
                    spec.argv,
                    cwd=spec.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    shell=False,
                    start_new_session=True,
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
            sink_fault = threading.Event()
            stdin_writer = _StdinWriter(stream=process.stdin, payload=stdin_payload)
            stdout_reader = _StreamReader(
                channel="stdout",
                stream=stdout_stream,
                sink=sink,
                sink_lock=sink_lock,
                sink_fault=sink_fault,
                clock=self._clock,
            )
            stderr_reader = _StreamReader(
                channel="stderr",
                stream=stderr_stream,
                sink=sink,
                sink_lock=sink_lock,
                sink_fault=sink_fault,
                clock=self._clock,
            )

            stdin_writer.start()
            stdout_reader.start()
            stderr_reader.start()

            return_code, timed_out, interrupted, logging_error = (
                _wait_for_exit_or_deadline(
                    process,
                    self._clock,
                    deadline_ns(start_ns, spec.timeout_seconds),
                    cancelled,
                    sink_fault,
                )
            )

            termination_confirmed = True
            if timed_out or interrupted or logging_error:
                return_code, termination_confirmed = _escalate_and_confirm_termination(
                    process, spec.termination_grace_seconds
                )
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)

        join_bound = spec.termination_grace_seconds
        stdin_writer.join(timeout=join_bound)
        stdout_reader.join(timeout=join_bound)
        stderr_reader.join(timeout=join_bound)
        if (
            stdin_writer.is_alive()
            or stdout_reader.is_alive()
            or stderr_reader.is_alive()
        ):
            termination_confirmed = False

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
            timed_out=timed_out,
            interrupted=interrupted,
            logging_error=logging_error,
            stdout_byte_count=stdout_byte_count,
            stdout_sha256=stdout_sha256,
            stderr_byte_count=stderr_byte_count,
            stderr_sha256=stderr_sha256,
            log_path=sink.path,
        )


__all__ = (
    "SubprocessRunner",
    "build_process_result",
    "deadline_ns",
    "sanitize_command",
)

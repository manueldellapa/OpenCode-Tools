"""The composition root: argv parsing, adapter construction, pipeline run.

`cli.py` is the *only* module authorized to load configuration, resolve
executables, construct concrete adapters, and inject them into
`orchestrator.py`'s pure, port-only functions (System Design SS5.2-5.3;
ADR-001). `build_parser`/`parse_args` own the v0.1 command shape only --
workspace/target/issue shape, a single positive issue number, and a
relative target are proven before any config file, adapter, or pipeline is
touched. `run_composed_pipeline` owns the actual composition: transforming
argv into a `RunRequest`/`AppConfig`, wiring every port `bootstrap_run`
needs, running the single-issue pipeline, and finalizing -- it never
duplicates `orchestrator.py`'s lifecycle, state machine, or retry decisions,
and it never mutates Git or GitHub. `main` is the only place real adapters
(subprocess-backed, filesystem-backed) get built; `run_composed_pipeline`
itself accepts every port already built, so a test can substitute fakes for
all of them (NFR-006) without this module ever knowing the difference.

`main` renders the canonical terminal contract (System Design SS13.4;
FR-047-FR-050): after a run was initialized, exactly one `FINAL_STATUS:
APPROVED|FAILED` line on stdout and nothing else there ever; a concise,
display-safe summary (run ID, last phase, terminal outcome, artifact path,
the last attempt's provider diagnostic when it left no terminal agent
response, persisted errors, and the preserved-changes inventory grouped by
staged/unstaged/untracked) on stderr; and the canonical exit code --
`IssueResult.expected_exit_code` when a run was initialized, `state_machine.
resolve_exit_code` applied to the raw error's own outcome otherwise. A
failure before a run directory could ever exist (including a pre-init
SIGINT) promises no artifact and prints no `FINAL_STATUS` line, per FR-047.
This module never recomputes precedence, outcome, or the final gate --
those stay `state_machine.py`/`orchestrator.py`'s alone; the provider
diagnostic line is a verbatim readout of `AttemptRecord.agent_result`'s own
already-decided fields, never a re-derivation of which attempt or outcome
was terminal.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import signal
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Protocol, cast, runtime_checkable

from opencode_tools import (
    __version__,
    coder_sandbox,
    git_safety,
    github,
    locking,
    runlog,
)
from opencode_tools import opencode as opencode_adapter
from opencode_tools.config import (
    build_run_request,
    load_app_config,
    sanitize_app_config,
)
from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    AppConfig,
    ErrorRecord,
    FinalStatus,
    FrozenJsonValue,
    GitCheckRecord,
    GithubTargetOverride,
    GitState,
    IssueLocator,
    IssueResult,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProviderDiagnostic,
    RepositoryIdentity,
    ReviewStatus,
    RunOutcome,
    RunRecord,
    RunRequest,
    TargetRepository,
    Workspace,
)
from opencode_tools.errors import (
    LoggingError,
    OpenCodeToolsError,
    ProtocolError,
    RunInterruptedError,
    to_error_records,
)
from opencode_tools.orchestrator import (
    IssuePipelineResult,
    LogicalInvocationResult,
    bootstrap_run,
    finalize_run,
    run_issue_pipeline,
)
from opencode_tools.ports import (
    AgentRunner,
    AttemptLogSink,
    Clock,
    GitSafetyPort,
    IssueResolver,
    LogChannel,
    OpenCodePreflightPort,
    ProcessRunner,
    RunStorePort,
    Sleeper,
    TargetLeaseFactory,
)
from opencode_tools.process import SubprocessRunner, build_process_result
from opencode_tools.protocol import parse_agent_response
from opencode_tools.state_machine import classify_attempt_outcome, resolve_exit_code

_PROG = "opencode-tools"

_PHASE_BY_ROLE: Mapping[AgentRole, PipelinePhase] = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}
_PROCESS_ERROR_LIKE_OUTCOMES = frozenset(
    {RunOutcome.PROCESS_ERROR, RunOutcome.LOGGING_ERROR, RunOutcome.INTERRUPTED}
)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"issue must be a positive integer: {raw!r}"
        ) from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"issue must be a positive integer: {raw!r}")
    return value


def _relative_target(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        raise argparse.ArgumentTypeError(
            f"target must be '.' or a relative path: {raw!r}"
        )
    return path


def build_parser() -> argparse.ArgumentParser:
    """Build the v0.1 command shape: a single `run` subcommand.

    `run` takes exactly `--workspace`, `--target`, `--issue`, and the
    optional `--config`; there are no lifecycle overrides, no batch/range
    issue forms, and no other subcommands in v0.1.
    """

    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Run the OpenCode Tools single-issue pipeline for one GitHub issue.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run the single-issue pipeline for one GitHub issue."
    )
    run_parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="OpenCode workspace root.",
    )
    run_parser.add_argument(
        "--target",
        required=True,
        type=_relative_target,
        help="Git target, relative to the workspace ('.' for the workspace root).",
    )
    run_parser.add_argument(
        "--issue",
        required=True,
        type=_positive_int,
        help="GitHub issue number (a positive integer).",
    )
    run_parser.add_argument(
        "--config",
        required=False,
        type=Path,
        default=None,
        help="Path to an explicit opencode-tools.toml config file.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse `argv` (default: `sys.argv[1:]`) against the v0.1 command shape."""

    return build_parser().parse_args(argv)


# --- real Clock/Sleeper: no fake/test double exists in src/ for either ----


class _SystemClock:
    """The real `Clock`: wall-clock UTC time and a monotonic counter."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


_SLEEPER_POLL_INTERVAL_SECONDS = 0.5


class _SystemSleeper:
    """The real `Sleeper`: a blocking `time.sleep`, polled in short
    increments rather than made in one call.

    A single `time.sleep(seconds)` would keep running for its own full
    *remaining* duration even after this process's SIGINT/SIGTERM handler
    observes a signal and returns without raising: per PEP 475, CPython
    retries an interrupted blocking call with the recomputed timeout rather
    than abandoning it, so setting a cancellation flag alone never actually
    shortens an in-progress `time.sleep`. Since `ProviderRetryConfig.
    max_delay_seconds` can be configured up to 1800 seconds (System Design
    SS12.2), a SIGINT arriving early in a long backoff delay would
    otherwise take up to that long to have any visible effect (issue
    #111). Sleeping in bounded `_SLEEPER_POLL_INTERVAL_SECONDS` chunks and
    checking `request_cancellation`'s flag between each one instead caps
    that latency at one poll interval, while still blocking for the full
    requested `seconds` -- the exact planned backoff delay -- when no
    cancellation ever arrives.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def request_cancellation(self) -> None:
        """Cut any `sleep` call already in progress short (issue #111)."""

        self._cancelled.set()

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._cancelled.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, _SLEEPER_POLL_INTERVAL_SECONDS))


@runtime_checkable
class _AcceptsCancellationRequest(Protocol):
    """A `Sleeper` (only `_SystemSleeper` in practice) that can cut a
    `sleep` already in progress short, given a live cancellation request --
    checked structurally so `run_composed_pipeline`'s signal handler can
    forward the request to whichever concrete `Sleeper` it was actually
    given without depending on `_SystemSleeper` by name, and without every
    test fake (a `RecordingSleeper` that never really sleeps) needing to
    implement a method it has no use for.
    """

    def request_cancellation(self) -> None: ...


# --- GitSafetyPort: binds process/executable/clock/timeouts to the three
# free functions git_safety.py exposes under different names/shapes -------


class _CliGitSafetyPort:
    def __init__(
        self,
        process_runner: ProcessRunner,
        *,
        git_executable: Path,
        clock: Clock,
        utility_timeout_seconds: float,
        termination_grace_seconds: float,
    ) -> None:
        self._process_runner = process_runner
        self._git_executable = git_executable
        self._clock = clock
        self._utility_timeout_seconds = utility_timeout_seconds
        self._termination_grace_seconds = termination_grace_seconds

    def check_runtime_location(self, runtime_root: Path) -> None:
        git_safety.check_runtime_location(
            self._process_runner,
            git_executable=self._git_executable,
            runtime_root=runtime_root,
            utility_timeout_seconds=self._utility_timeout_seconds,
            termination_grace_seconds=self._termination_grace_seconds,
        )

    def resolve_target(
        self, workspace: Workspace, target_root: Path
    ) -> TargetRepository:
        return git_safety.resolve_target(
            self._process_runner,
            git_executable=self._git_executable,
            workspace=workspace,
            target_root=target_root,
            utility_timeout_seconds=self._utility_timeout_seconds,
            termination_grace_seconds=self._termination_grace_seconds,
        )

    def check(
        self,
        target: TargetRepository,
        *,
        sequence: int,
        purpose: str,
        role: AgentRole | None = None,
        baseline: GitState | None = None,
    ) -> GitCheckRecord:
        return git_safety.check_git_state(
            self._process_runner,
            git_executable=self._git_executable,
            target=target,
            clock=self._clock,
            sequence=sequence,
            purpose=purpose,
            role=role,
            baseline=baseline,
            utility_timeout_seconds=self._utility_timeout_seconds,
            termination_grace_seconds=self._termination_grace_seconds,
        )


# --- IssueResolver: github.py deliberately stays free-function-only ------


class _GhCliIssueResolver:
    def __init__(
        self,
        process_runner: ProcessRunner,
        *,
        git_executable: Path,
        gh_executable: Path,
        github_targets: tuple[GithubTargetOverride, ...],
        utility_timeout_seconds: float,
        termination_grace_seconds: float,
    ) -> None:
        self._process_runner = process_runner
        self._git_executable = git_executable
        self._gh_executable = gh_executable
        self._github_targets = github_targets
        self._utility_timeout_seconds = utility_timeout_seconds
        self._termination_grace_seconds = termination_grace_seconds

    def resolve_repository(self, target: TargetRepository) -> RepositoryIdentity:
        return github.resolve_repository_identity(
            self._process_runner,
            git_executable=self._git_executable,
            target=target,
            github_targets=self._github_targets,
            utility_timeout_seconds=self._utility_timeout_seconds,
            termination_grace_seconds=self._termination_grace_seconds,
        )

    def locate_issue(
        self, repository_identity: RepositoryIdentity, issue_number: int
    ) -> IssueLocator:
        return github.locate_issue(
            repository_identity,
            issue_number,
            process_runner=self._process_runner,
            gh_executable=self._gh_executable,
            utility_timeout_seconds=self._utility_timeout_seconds,
            termination_grace_seconds=self._termination_grace_seconds,
        )


# --- RunStorePort: runlog.py exposes the run-directory/sink/persist steps
# as free functions plus one AttemptLogSink class, never a full port ------


class _CliRunStore:
    """Also remembers the last `RunRecord` it was asked to persist.

    `finalize_run` always calls `persist` exactly once more, with the true
    terminal record, before returning an `IssueResult` -- which itself
    carries no phase, Git-state, or error detail (System Design SS15.3 vs.
    the deliberately minimal `IssueResult`). `main`'s rendering reads that
    remembered record back through `last_record` rather than re-deriving
    any of it, so this stays a passive recollection, never a second
    decision about phase, outcome, or the final gate.
    """

    def __init__(self, *, runtime_root: Path, clock: Clock) -> None:
        self._runtime_root = runtime_root
        self._clock = clock
        self._run_directory: Path | None = None
        self._last_record: RunRecord | None = None
        self._staged_final_record: RunRecord | None = None
        self._staged_final_path: Path | None = None

    @property
    def last_record(self) -> RunRecord | None:
        return self._last_record

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        del workspace
        run_directory = runlog.create_run_directory(self._runtime_root, run_id)
        self._run_directory = run_directory
        return run_directory

    def open_attempt_sink(
        self,
        role: AgentRole,
        review_cycle: int | None,
        provider_attempt: int,
    ) -> AttemptLogSink:
        if self._run_directory is None:
            raise AssertionError("initialize must be called before open_attempt_sink")
        filename = runlog.attempt_log_filename(role, review_cycle, provider_attempt)
        return runlog.AttemptLogFileSink(self._run_directory, filename, self._clock)

    def persist(self, record: RunRecord) -> PersistenceStatus:
        self._last_record = record
        try:
            runlog.persist_run_record(record)
        except LoggingError:
            return PersistenceStatus.FAILED
        return PersistenceStatus.OK

    def stage_final(self, record: RunRecord) -> PersistenceStatus:
        """Prepare, but do not publish, the terminal record (issue #129)."""

        try:
            staged_path = runlog.prepare_run_record(record)
        except LoggingError:
            return PersistenceStatus.FAILED

        previous_path = self._staged_final_path
        self._staged_final_record = record
        self._staged_final_path = staged_path
        if previous_path is not None:
            runlog.discard_prepared_run_record(previous_path)
        return PersistenceStatus.OK

    def commit_final(self) -> PersistenceStatus:
        """Publish the staged terminal record with one canonical replace."""

        record = self._staged_final_record
        staged_path = self._staged_final_path
        if record is None or staged_path is None:
            raise AssertionError("stage_final must succeed before commit_final")

        try:
            runlog.commit_prepared_run_record(record, staged_path)
        except LoggingError:
            self._staged_final_record = None
            self._staged_final_path = None
            return PersistenceStatus.FAILED

        self._last_record = record
        self._staged_final_record = None
        self._staged_final_path = None
        return PersistenceStatus.OK

    def abort_final(self) -> None:
        """Discard an unpublished terminal candidate, idempotently."""

        if self._staged_final_path is not None:
            runlog.discard_prepared_run_record(self._staged_final_path)
        self._staged_final_record = None
        self._staged_final_path = None


# --- OpenCodePreflightPort: caches the digest so a caller that also wants
# to `.verify()` ahead of bootstrap never re-runs the real preflight ------


class _CliOpenCodePreflightPort:
    def __init__(
        self,
        process_runner: ProcessRunner,
        *,
        executable: Path,
        workspace: Workspace,
        target_root: Path,
        utility_timeout_seconds: float,
        termination_grace_seconds: float,
    ) -> None:
        self._process_runner = process_runner
        self._executable = executable
        self._workspace = workspace
        self._target_root = target_root
        self._utility_timeout_seconds = utility_timeout_seconds
        self._termination_grace_seconds = termination_grace_seconds
        self._digest: str | None = None

    def verify(self) -> str:
        if self._digest is None:
            evidence = opencode_adapter.run_preflight(
                self._process_runner,
                executable=self._executable,
                workspace=self._workspace,
                target_root=self._target_root,
                utility_timeout_seconds=self._utility_timeout_seconds,
                termination_grace_seconds=self._termination_grace_seconds,
            )
            self._digest = evidence.control_plane_digest
        return self._digest

    def recheck(self, expected_digest: str) -> None:
        opencode_adapter.recheck_control_plane(
            self._process_runner,
            executable=self._executable,
            workspace=self._workspace,
            target_root=self._target_root,
            utility_timeout_seconds=self._utility_timeout_seconds,
            termination_grace_seconds=self._termination_grace_seconds,
            expected_digest=expected_digest,
        )


# --- AgentRunner: the first concrete implementation anywhere in this repo.
# Tees the real sink into an in-memory capture (opencode.py forbids ever
# persisting raw run output) so this adapter can decode/classify/parse what
# the child printed, then verifies agent identity via a follow-up export. --


@runtime_checkable
class _CaptureSink(Protocol):
    """Structural shape of `opencode.open_run_capture_sink`'s return value."""

    def write(
        self, channel: LogChannel, payload: bytes, timestamp: datetime
    ) -> None: ...
    def bytes_for(self, channel: LogChannel) -> bytes: ...
    def overflowed(self, channel: LogChannel) -> bool: ...
    def close(self) -> None: ...


class _TeeSink:
    """Fans one attempt's stream out to the durable sink and the in-memory
    capture the OpenCode adapter reads back -- never persisted raw itself
    (System Design SS10.1/SS18.3; `opencode._BoundedCapturingSink` docstring).
    """

    def __init__(self, sink: AttemptLogSink, capture: _CaptureSink) -> None:
        self._sink = sink
        self._capture = capture

    @property
    def path(self) -> Path:
        return self._sink.path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        self._sink.write(channel, payload, timestamp)
        self._capture.write(channel, payload, timestamp)

    def close(self) -> None:
        self._sink.close()
        self._capture.close()


@runtime_checkable
class _AcceptsTargetRepository(Protocol):
    """An AgentRunner that needs the validated target bound after bootstrap."""

    def bind_target(self, target: TargetRepository) -> None: ...


@runtime_checkable
class _AcceptsIssueLocator(Protocol):
    """An `AgentRunner` that needs `issue_locator` bound after bootstrap.

    `IssueLocator` cannot be known when the `AgentRunner` is constructed --
    `bootstrap_run` resolves it only *after* receiving the already-built
    runner as one of its own arguments -- so this is set once, by
    `run_composed_pipeline`, strictly before any role is ever invoked.
    """

    def bind_issue_locator(self, issue_locator: IssueLocator) -> None: ...


def _decode_utf8_lenient(payload: bytes) -> str | None:
    if not payload:
        return None
    return payload.decode("utf-8", errors="replace")


class _CliAgentRunner:
    def __init__(
        self,
        process_runner: ProcessRunner,
        *,
        executable: Path,
        target_root: Path,
        opencode_timeout_seconds: float,
        utility_timeout_seconds: float,
        termination_grace_seconds: float,
        git_executable: Path | None = None,
        clock: Clock | None = None,
        sandbox_coder: bool = True,
    ) -> None:
        self._process_runner = process_runner
        self._executable = executable
        self._target_root = target_root
        self._opencode_timeout_seconds = opencode_timeout_seconds
        self._utility_timeout_seconds = utility_timeout_seconds
        self._termination_grace_seconds = termination_grace_seconds
        self._git_executable = git_executable
        self._clock = clock
        self._sandbox_coder = sandbox_coder
        self._issue_locator: IssueLocator | None = None
        self._target: TargetRepository | None = None

    def bind_issue_locator(self, issue_locator: IssueLocator) -> None:
        self._issue_locator = issue_locator

    def bind_target(self, target: TargetRepository) -> None:
        self._target = target

    def _decode_transport(
        self, stdout_bytes: bytes, *, overflowed: bool
    ) -> opencode_adapter.TransportResult | None:
        try:
            return opencode_adapter.decode_run_output(
                stdout_bytes, overflowed=overflowed
            )
        except ProtocolError:
            return None

    def _parse_response(
        self, role: AgentRole, text: str, *, issue_locator: IssueLocator
    ) -> ParsedAgentResponse | None:
        try:
            return parse_agent_response(role, text, issue_locator=issue_locator)
        except ProtocolError:
            return None

    def _verify_identity(
        self, role: AgentRole, workspace: Workspace, session_id: str
    ) -> tuple[str | None, str | None]:
        """Return verified agent plus a sanitized failure code, fail closed.

        The export itself remains in-memory only.  On verification failure we
        retain only the stable internal ProtocolError code so run evidence can
        distinguish call, schema, and identity failures without persisting the
        sanitized session export or reclassifying the failure as provider
        retryable.
        """

        try:
            evidence = opencode_adapter.run_export_and_verify_identity(
                self._process_runner,
                executable=self._executable,
                workspace=workspace,
                role=role,
                session_id=session_id,
                utility_timeout_seconds=self._utility_timeout_seconds,
                termination_grace_seconds=self._termination_grace_seconds,
            )
        except ProtocolError as error:
            return None, error.code
        return evidence.verified_agent, None

    def _sandbox_failure_result(
        self,
        *,
        review_cycle: int | None,
        provider_attempt: int,
        sink: AttemptLogSink,
        message: str,
    ) -> AgentResult:
        if self._clock is None or self._git_executable is None:
            raise AssertionError("sandbox dependencies must be configured")
        timestamp = self._clock.now()
        sink.write(
            "stderr",
            f"coder sandbox failure: {message}\n".encode(),
            timestamp,
        )
        empty_sha = hashlib.sha256(b"").hexdigest()
        process_result = build_process_result(
            command=(str(self._git_executable), "coder-sandbox"),
            cwd=self._target_root,
            started_at=timestamp,
            finished_at=timestamp,
            duration_ns=0,
            return_code=None,
            termination_confirmed=True,
            stdout_byte_count=0,
            stdout_sha256=empty_sha,
            stderr_byte_count=0,
            stderr_sha256=empty_sha,
            log_path=sink.path,
        )
        return AgentResult(
            role=AgentRole.CODER,
            phase=PipelinePhase.CODER,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            process=process_result,
            terminal_response=None,
            session_id=None,
            verified_agent=None,
            provider_diagnostic=None,
            outcome=RunOutcome.PROCESS_ERROR,
        )

    def run(
        self,
        role: AgentRole,
        prompt: str,
        workspace: Workspace,
        *,
        review_cycle: int | None,
        provider_attempt: int,
        sink: AttemptLogSink,
    ) -> AgentResult:
        if self._issue_locator is None:
            raise AssertionError("bind_issue_locator must be called before run")

        sandbox: coder_sandbox.CoderSandbox | None = None
        execution_root = self._target_root
        if role is AgentRole.CODER and self._sandbox_coder:
            if (
                self._target is None
                or self._git_executable is None
                or self._clock is None
            ):
                raise AssertionError(
                    "bind_target and sandbox dependencies are required for coder"
                )
            try:
                sandbox = coder_sandbox.prepare_coder_sandbox(
                    self._process_runner,
                    git_executable=self._git_executable,
                    target=self._target,
                    clock=self._clock,
                    utility_timeout_seconds=self._utility_timeout_seconds,
                    termination_grace_seconds=self._termination_grace_seconds,
                )
            except coder_sandbox.CoderSandboxError as error:
                return self._sandbox_failure_result(
                    review_cycle=review_cycle,
                    provider_attempt=provider_attempt,
                    sink=sink,
                    message=str(error),
                )
            execution_root = sandbox.root

        try:
            spec = opencode_adapter.build_run_spec(
                self._executable,
                role,
                prompt,
                workspace,
                target_root=execution_root,
                timeout_seconds=self._opencode_timeout_seconds,
                termination_grace_seconds=self._termination_grace_seconds,
            )
            cycle_component = review_cycle if review_cycle is not None else 0
            capture = opencode_adapter.open_run_capture_sink(
                f"{role.value.lower()}-{cycle_component}-{provider_attempt}-capture"
            )
            process_result = self._process_runner.run(
                spec, sink=_TeeSink(sink, capture)
            )

            stdout_bytes = capture.bytes_for("stdout")
            stdout_text = _decode_utf8_lenient(stdout_bytes)
            provider_diagnostic = (
                opencode_adapter.classify_provider_signal(stdout_text)
                if stdout_text is not None
                else None
            )

            session_id: str | None = None
            terminal_response: ParsedAgentResponse | None = None
            verified_agent: str | None = None
            identity_verification_error_code: str | None = None

            if provider_diagnostic is None and not process_result.timed_out:
                transport = self._decode_transport(
                    stdout_bytes, overflowed=capture.overflowed("stdout")
                )
                if transport is not None:
                    session_id = transport.session_id
                    terminal_response = self._parse_response(
                        role,
                        transport.terminal_text,
                        issue_locator=self._issue_locator,
                    )
                    (
                        verified_agent,
                        identity_verification_error_code,
                    ) = self._verify_identity(role, workspace, session_id)
                    if identity_verification_error_code is not None:
                        terminal_response = None

            if (
                role is AgentRole.CODER
                and sandbox is not None
                and provider_diagnostic is None
                and not process_result.timed_out
                and process_result.outcome is RunOutcome.SUCCEEDED
                and terminal_response is not None
                and terminal_response.agent_status is not AgentStatus.FAILED
            ):
                assert self._target is not None
                assert self._git_executable is not None
                assert self._clock is not None
                try:
                    coder_sandbox.promote_coder_changes(
                        self._process_runner,
                        git_executable=self._git_executable,
                        target=self._target,
                        sandbox=sandbox,
                        clock=self._clock,
                        utility_timeout_seconds=self._utility_timeout_seconds,
                        termination_grace_seconds=self._termination_grace_seconds,
                    )
                except coder_sandbox.CoderSandboxError as error:
                    sink.write(
                        "stderr",
                        (f"coder sandbox promotion blocked: {error}\n").encode(),
                        self._clock.now(),
                    )
                    terminal_response = None

            timed_out = process_result.timed_out
            provider_error = provider_diagnostic is not None
            process_error = process_result.outcome in _PROCESS_ERROR_LIKE_OUTCOMES
            higher_precedence_signal = timed_out or provider_error or process_error
            protocol_error = not higher_precedence_signal and terminal_response is None
            agent_reported_failure = (
                not higher_precedence_signal
                and terminal_response is not None
                and terminal_response.agent_status is AgentStatus.FAILED
            )
            succeeded = (
                not higher_precedence_signal
                and terminal_response is not None
                and terminal_response.agent_status is not AgentStatus.FAILED
            )
            precedence = classify_attempt_outcome(
                timed_out=timed_out,
                provider_error=provider_error,
                process_error=process_error,
                protocol_error=protocol_error,
                agent_reported_failure=agent_reported_failure,
                succeeded=succeeded,
            )

            return AgentResult(
                role=role,
                phase=_PHASE_BY_ROLE[role],
                review_cycle=review_cycle,
                provider_attempt=provider_attempt,
                process=process_result,
                terminal_response=terminal_response,
                session_id=session_id,
                verified_agent=verified_agent,
                provider_diagnostic=provider_diagnostic,
                outcome=precedence.outcome,
                identity_verification_error_code=identity_verification_error_code,
            )
        finally:
            if sandbox is not None:
                coder_sandbox.cleanup_coder_sandbox(sandbox)


def _environment_snapshot() -> Mapping[str, FrozenJsonValue]:
    """The version-fact subset knowable before any adapter I/O ever runs.

    System Design SS15.3 documents `opencode_version`/`git_version`/
    `gh_version` alongside these three, but every one of them is only ever
    discovered *inside* `bootstrap_run` (issue/repository resolution and
    OpenCode preflight, steps 5-6) -- strictly after the first `RunRecord`
    this snapshot feeds into (step 3) has already been persisted, and
    `RunRecord.environment` is never amended afterward. Populating them
    here would mean re-running those same probes a second time outside the
    ports `bootstrap_run` already owns, duplicating adapter logic this
    milestone must not reimplement.
    """

    return {
        "tool_version": __version__,
        "python_version": platform.python_version(),
        "platform": sys.platform,
    }


def _trigger_outcome(result: IssuePipelineResult) -> RunOutcome:
    return result.outcome if result.outcome is not None else RunOutcome.SUCCEEDED


def _all_attempts(result: IssuePipelineResult) -> tuple[LogicalInvocationResult, ...]:
    return (
        *result.architect,
        *(attempt for cycle in result.coder_cycles for attempt in cycle),
        *(attempt for cycle in result.reviewer_cycles for attempt in cycle),
    )


def _last_review_status(result: IssuePipelineResult) -> ReviewStatus | None:
    if not result.reviewer_cycles:
        return None
    last_attempt = result.reviewer_cycles[-1][-1]
    if (
        last_attempt.agent_result is None
        or last_attempt.agent_result.terminal_response is None
    ):
        return None
    return last_attempt.agent_result.terminal_response.review_status


def _termination_confirmed(result: IssuePipelineResult) -> bool | None:
    confirmed_values = [
        attempt.agent_result.process.termination_confirmed
        for attempt in _all_attempts(result)
        if attempt.agent_result is not None
    ]
    if not confirmed_values:
        return None
    return all(value is True for value in confirmed_values)


def _interrupted(result: IssuePipelineResult) -> bool:
    return any(
        attempt.agent_result.process.outcome is RunOutcome.INTERRUPTED
        for attempt in _all_attempts(result)
        if attempt.agent_result is not None
    )


def run_composed_pipeline(
    *,
    run_request: RunRequest,
    app_config: AppConfig,
    run_id: str,
    process_runner: ProcessRunner,
    clock: Clock,
    sleeper: Sleeper,
    git_safety_port: GitSafetyPort,
    issue_resolver: IssueResolver,
    run_store: RunStorePort,
    lease_factory: TargetLeaseFactory,
    opencode_preflight: OpenCodePreflightPort,
    agent_runner: AgentRunner,
) -> IssueResult | OpenCodeToolsError:
    """Compose bootstrap, the single-issue pipeline, and finalization.

    Every port is accepted already-built, exactly mirroring
    `orchestrator.bootstrap_run`'s own injection shape, so a unit test can
    substitute fakes for every dependency without this function ever
    knowing whether it is talking to a real adapter or a test double
    (NFR-006). `main` builds the *real* adapters from `app_config`; that
    construction is kept out of this function so composition wiring stays
    testable in isolation, with pre-init failure/factory fakes, per this
    issue's own test requirements.
    """

    config_snapshot = cast(
        "Mapping[str, FrozenJsonValue]", sanitize_app_config(app_config)
    )

    outcome = bootstrap_run(
        run_request=run_request,
        config=app_config,
        run_id=run_id,
        config_snapshot=config_snapshot,
        environment_snapshot=_environment_snapshot(),
        git_safety=git_safety_port,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=opencode_preflight,
        agent_runner=agent_runner,
        clock=clock,
        sleeper=sleeper,
    )

    if outcome.error is not None:
        if outcome.issue_result is not None:
            return outcome.issue_result
        if outcome.lease is not None:
            # `bootstrap_run` documents that it never releases or
            # quarantines a lease it acquired itself on an early failure
            # (before a run directory -- and so `finalize_run` -- ever
            # existed); this is the one path where this composition root,
            # not `finalize_run`, must release it.
            outcome.lease.__exit__(None, None, None)
        return outcome.error

    if (
        outcome.orchestrator is None
        or outcome.issue_locator is None
        or outcome.record is None
    ):
        raise AssertionError(
            "bootstrap_run must set orchestrator/issue_locator/record when error is None"
        )

    orchestrator = outcome.orchestrator
    target = outcome.record.target
    if isinstance(agent_runner, _AcceptsIssueLocator):
        agent_runner.bind_issue_locator(outcome.issue_locator)
    if isinstance(agent_runner, _AcceptsTargetRepository):
        agent_runner.bind_target(target)

    def _request_pipeline_cancellation(
        signal_number: int, frame: FrameType | None
    ) -> None:
        del signal_number, frame
        orchestrator.request_cancellation()
        # A plain `time.sleep` already in progress (`run_provider_attempts`'
        # own backoff delay) would otherwise keep running for its own full
        # remaining duration regardless -- up to `ProviderRetryConfig.
        # max_delay_seconds`, 1800s -- since setting `cancellation_
        # requested` above has no effect on a blocking call already under
        # way. `sleeper` is only ever `_SystemSleeper` in production; a
        # test fake with no use for this simply does not implement it.
        if isinstance(sleeper, _AcceptsCancellationRequest):
            sleeper.request_cancellation()

    # A SIGINT/SIGTERM arriving anywhere in `run_issue_pipeline` -- most
    # notably during `run_provider_attempts`' own backoff sleep between
    # provider retries, but equally any other idle window between one
    # logical invocation and the next -- must not unwind as a raw Python
    # `KeyboardInterrupt`/default `SIGTERM` disposition (System Design
    # SH-001): `run.json` already exists by this point (`bootstrap_run`
    # returned), so that would be misreported by `main`'s own pre-init
    # handlers exactly like issue #111 describes. Installing this handler
    # here, once `orchestrator` exists, makes it the "previous" handler
    # `SubprocessRunner.run` itself saves and restores around each child
    # call (`process.py`), so the two compose without any change there:
    # during a live child, `process.py`'s own escalation still applies;
    # between children, this handler simply records the request so the
    # next `run_logical_invocation` raises `RunInterruptedError` (or, if a
    # retry's backoff sleep is what is interrupted, so that its next
    # attempt does) instead of a bare `KeyboardInterrupt` ever reaching
    # `main`.
    previous_sigint = signal.signal(signal.SIGINT, _request_pipeline_cancellation)
    previous_sigterm = signal.signal(signal.SIGTERM, _request_pipeline_cancellation)
    try:
        pipeline_result = run_issue_pipeline(
            orchestrator=orchestrator,
            issue_locator=outcome.issue_locator,
            workspace=run_request.workspace,
            target=target,
            max_review_cycles=app_config.execution.max_review_cycles,
        )
    except OpenCodeToolsError as error:
        # A `LoggingError`/`RunInterruptedError` raised mid-pipeline (System
        # Design SS15.4: a broken persistence layer or a caught signal)
        # propagates out of `run_issue_pipeline` by design, uncaught by any
        # intermediate layer -- but a run directory already exists at this
        # point (bootstrap already succeeded), so this is a terminal path
        # "successivo alla run init" exactly like `bootstrap_run`'s own
        # late-stage failures, and must converge through the same
        # `finalize_run` rather than escape to `main`'s pre-init handler.
        # The caught error itself is folded into `errors` first (mirroring
        # `bootstrap_run`'s own late-stage except-handler and `finalize_run`'s
        # own quarantine-failure handling), so both the persisted artifact
        # and `_render_issue_result`'s stderr summary keep the actual
        # diagnosis instead of only the bare terminal outcome.
        last_record = outcome.orchestrator.record
        record_with_error = replace(
            last_record,
            errors=(
                *last_record.errors,
                *to_error_records(
                    error,
                    # `last_record.current_phase` would still name the last
                    # role that happened to persist (e.g. `ARCHITECT`) when
                    # a *later* role's own `open_attempt_sink`/`persist` is
                    # what actually raised -- `last_attempted_phase` always
                    # names the invocation this error truly belongs to.
                    phase=outcome.orchestrator.last_attempted_phase,
                    timestamp=clock.now(),
                    first_sequence=(
                        last_record.errors[-1].sequence + 1 if last_record.errors else 0
                    ),
                ),
            ),
            # `last_record` was the last snapshot durably persisted *before*
            # this invocation -- it carries `persistence_status=OK` and
            # `artifact_incomplete=False` verbatim, even though a
            # `LoggingError` here means the interrupted attempt's own
            # record/log never became durable either. Marking the artifact
            # incomplete (rather than leaving it `OK`) is what lets
            # `finalize_run` still report it as such even when its own
            # later write of `run.json` itself succeeds -- scoped to
            # `LoggingError` specifically (not every `OpenCodeToolsError`
            # this branch can catch) so an unrelated secondary error, e.g.
            # after an already-observed interruption, never gets outranked
            # by a spurious `LOGGING_ERROR` cause in `resolve_terminal_
            # outcome`'s precedence.
            persistence_status=(
                PersistenceStatus.INCOMPLETE
                if isinstance(error, LoggingError)
                else last_record.persistence_status
            ),
            artifact_incomplete=(
                True
                if isinstance(error, LoggingError)
                else last_record.artifact_incomplete
            ),
        )
        result = finalize_run(
            record=record_with_error,
            target=target,
            trigger_outcome=error.outcome,
            review_status=None,
            # A plain `isinstance(error, RunInterruptedError)` would miss an
            # interruption already observed on this same invocation (the
            # agent's own process outcome was `INTERRUPTED`) when a
            # *different* `OpenCodeToolsError` is what a later operation in
            # that same invocation -- the after-attempt Git check, the sink
            # close, or the post-attempt `persist` itself -- went on to
            # raise; `cancellation_requested` still reflects that fact.
            interrupted=(
                isinstance(error, RunInterruptedError)
                or outcome.orchestrator.cancellation_requested
            ),
            # `record` alone would lose the failed attempt's own
            # termination evidence when a post-attempt `persist` is exactly
            # what raised `error` -- `last_observed_termination_confirmed`
            # survives that loss, so a possibly still-live child still
            # forces postflight `INDETERMINATE` and quarantines the lease.
            termination_confirmed=outcome.orchestrator.last_observed_termination_confirmed,
            git_safety=git_safety_port,
            run_store=run_store,
            lease=outcome.lease,
            clock=clock,
            max_review_cycles=app_config.execution.max_review_cycles,
            # `record_with_error` is the last *durably persisted* snapshot
            # -- missing exactly the attempt whose own `open_attempt_sink`/
            # `persist` failure is why we are here, even though that
            # attempt's `SAFE` `after` checkpoint was already accepted
            # first. Deriving the checkpoint from `record_with_error` alone
            # would compare postflight against a checkpoint one attempt too
            # old, falsely reporting an authorized Git delta as `UNSAFE`.
            last_accepted_git_state=outcome.orchestrator.last_accepted_git_state,
            # A SIGINT/SIGTERM can still arrive during *this* `finalize_run`
            # call's own postflight probe, after `interrupted` above was
            # already decided -- `late_cancellation_check` lets it upgrade
            # that decision instead of being silently absorbed (issue #111).
            late_cancellation_check=lambda: orchestrator.cancellation_requested,
            seal_cancellation=orchestrator.seal_cancellation,
        )
    else:
        result = finalize_run(
            record=outcome.orchestrator.record,
            target=target,
            trigger_outcome=_trigger_outcome(pipeline_result),
            review_status=_last_review_status(pipeline_result),
            # `_interrupted(pipeline_result)` alone only sees a live child's
            # own `INTERRUPTED` process outcome -- it misses a cancellation
            # requested *after* the terminal attempt's own agent process
            # already returned (e.g. during that attempt's after-attempt
            # Git check, sink close, or `persist`, all of which run past
            # `process.py`'s own per-subprocess handler, with this
            # function's own handler active instead); `cancellation_
            # requested` still reflects that fact (issue #111).
            interrupted=(
                _interrupted(pipeline_result) or orchestrator.cancellation_requested
            ),
            termination_confirmed=_termination_confirmed(pipeline_result),
            git_safety=git_safety_port,
            run_store=run_store,
            lease=outcome.lease,
            clock=clock,
            max_review_cycles=app_config.execution.max_review_cycles,
            # See the except-branch call above: a SIGINT/SIGTERM can still
            # arrive during this `finalize_run` call's own postflight probe.
            late_cancellation_check=lambda: orchestrator.cancellation_requested,
            seal_cancellation=orchestrator.seal_cancellation,
        )
    finally:
        # Kept installed through *both* branches' own `finalize_run` call
        # above, not just `run_issue_pipeline` -- `finalize_run` still does
        # real, non-instant work afterward (a postflight Git probe,
        # persisting the terminal record, releasing/quarantining the
        # lease), and restoring the handler any earlier would reopen the
        # exact post-bootstrap idle window issue #111 closed: a SIGINT
        # there would again unwind as a raw `KeyboardInterrupt` past this
        # function, misreported by `main`'s pre-init handler even though
        # the run is fully finalized. Only once this function is entirely
        # done with `orchestrator` is the previous handler restored.
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)

    return result


def _flatten_error_causes(
    error: OpenCodeToolsError,
) -> tuple[OpenCodeToolsError, ...]:
    flattened: list[OpenCodeToolsError] = []
    for cause in error.causes:
        flattened.extend(_flatten_error_causes(cause))
    flattened.append(error)
    return tuple(flattened)


def _render_error_line(*, code: str, message: str, technical_detail: str | None) -> str:
    detail = f" ({technical_detail})" if technical_detail else ""
    return f"error [{code}]: {message}{detail}"


def _render_raw_error(error: OpenCodeToolsError) -> tuple[str, ...]:
    """Render `error` and every cause, earliest first (never a traceback or
    raw stderr -- `errors.py` already restricted these fields to sanitized,
    action-oriented text, including a compatibility failure's detected
    version and supported baseline)."""

    return tuple(
        _render_error_line(
            code=item.code, message=item.message, technical_detail=item.technical_detail
        )
        for item in _flatten_error_causes(error)
    )


def _render_error_records(errors: tuple[ErrorRecord, ...]) -> tuple[str, ...]:
    return tuple(
        _render_error_line(
            code=item.code, message=item.message, technical_detail=item.technical_detail
        )
        for item in errors
    )


def _render_git_state_summary(state: GitState) -> tuple[str, ...]:
    """Group preserved changes by staged/unstaged/untracked -- counts and
    paths only, per System Design SS13.4: never diff content or quality."""

    counts = (
        f"staged={len(state.staged)} unstaged={len(state.unstaged)} "
        f"untracked={len(state.untracked)}"
    )
    lines = [f"changes: {counts}"]
    if state.staged:
        lines.append(f"  staged: {', '.join(state.staged)}")
    if state.unstaged:
        lines.append(f"  unstaged (tracked): {', '.join(state.unstaged)}")
    if state.untracked:
        lines.append(f"  untracked: {', '.join(state.untracked)}")
    return tuple(lines)


def _render_provider_diagnostic_summary(diagnostic: ProviderDiagnostic) -> str:
    """Render a `ProviderDiagnostic` as one display-safe summary line.

    A verbatim field readout -- `signature`/`source`/`code`/`status_code`
    are already-decided facts on the persisted record, never recomputed or
    reclassified here."""

    status_part = (
        f" status={diagnostic.status_code}"
        if diagnostic.status_code is not None
        else ""
    )
    return (
        f"provider diagnostic: {diagnostic.signature} "
        f"(source={diagnostic.source} code={diagnostic.code}{status_part})"
    )


def _last_attempt_unexplained_provider_diagnostic(
    last_record: RunRecord | None,
) -> ProviderDiagnostic | None:
    """Return the last attempt's `ProviderDiagnostic` iff it left no
    terminal agent response, straight off the persisted `AttemptRecord` --
    never a re-derivation of `classify_attempt_outcome`'s own precedence,
    only a read of the two fields that already answer this question."""

    if last_record is None or not last_record.attempts:
        return None
    last_agent_result = last_record.attempts[-1].agent_result
    if last_agent_result.terminal_response is not None:
        return None
    return last_agent_result.provider_diagnostic


def _print_stderr_lines(lines: tuple[str, ...]) -> None:
    for line in lines:
        print(line, file=sys.stderr)


def _render_pre_init_failure(error: OpenCodeToolsError) -> int:
    """A failure before any run directory could exist (FR-047/AC-025): no
    `FINAL_STATUS` line, no artifact promise -- only a stderr diagnosis."""

    lines = (
        f"phase: {PipelinePhase.PREFLIGHT.value}",
        f"terminal outcome: {error.outcome.value}",
        "artifact: none (failed before a run could be initialized)",
        *_render_raw_error(error),
    )
    _print_stderr_lines(lines)
    return resolve_exit_code(
        final_status=FinalStatus.FAILED, terminal_outcome=error.outcome
    )


def _render_preinit_interrupted() -> int:
    _print_stderr_lines(
        ("interrupted before the run could be initialized; no artifact was created.",)
    )
    return 130


def _render_issue_result(result: IssueResult, *, last_record: RunRecord | None) -> int:
    """The canonical terminal contract for an initialized run: exactly one
    `FINAL_STATUS` line on stdout, a display-safe summary on stderr --
    including the last attempt's provider diagnostic when it left no
    terminal agent response (issue #80) -- and `IssueResult.
    expected_exit_code` (already the full precedence/gate decision --
    never recomputed here)."""

    print(f"FINAL_STATUS: {result.final_status.value}")

    lines = [
        f"run_id: {result.run_id}",
        f"phase: {PipelinePhase.FINISHED.value}",
        f"terminal outcome: {result.trigger_outcome.value}",
        f"artifact: {result.artifact_path}",
    ]
    if result.persistence_status is PersistenceStatus.FAILED:
        lines.append(
            "warning: final persistence failed; the run artifact may be incomplete."
        )
    elif result.persistence_status is PersistenceStatus.INCOMPLETE:
        # Distinct from `FAILED`: `run.json` itself was written -- an
        # *earlier* attempt's own record or log is what never became
        # durable, not this final write, so a caller must not read this as
        # "the final write failed" (it did not).
        lines.append(
            "warning: the run artifact is incomplete; an earlier attempt's "
            "own record or log was not safely finalized."
        )

    git_state: GitState | None = None
    if last_record is not None:
        if last_record.git_postflight is not None:
            git_state = last_record.git_postflight.state
        elif last_record.git_baseline is not None:
            git_state = last_record.git_baseline
    if git_state is not None:
        lines.append(
            f"changes preserved: {'yes' if result.changes_preserved else 'no'}"
        )
        lines.extend(_render_git_state_summary(git_state))

    diagnostic = _last_attempt_unexplained_provider_diagnostic(last_record)
    if diagnostic is not None:
        lines.append(_render_provider_diagnostic_summary(diagnostic))

    if last_record is not None and last_record.errors:
        lines.extend(_render_error_records(last_record.errors))

    _print_stderr_lines(tuple(lines))
    return result.expected_exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: validate argv shape, compose adapters, run the pipeline.

    An invalid `argv` is rejected by `argparse` itself with exit code 2,
    before anything else runs -- and, like a pre-init SIGINT, promises
    neither an artifact nor a `FINAL_STATUS` line. On a syntactically valid
    parse, this builds every real adapter and runs the single-issue
    pipeline through `run_composed_pipeline`, then renders its result via
    `_render_issue_result` (a run was initialized) or `_render_pre_init_
    failure` (it never got that far) for the full canonical exit-code table
    (0/2/10/20/30/40/130; System Design SS13.4).
    """

    args = parse_args(argv)
    cwd = Path.cwd()
    run_store: _CliRunStore | None = None

    try:
        run_request = build_run_request(
            issue_number=args.issue,
            workspace=args.workspace,
            target=args.target,
            cwd=cwd,
        )
        app_config = load_app_config(
            config_path=args.config,
            workspace=run_request.workspace,
            cwd=cwd,
        )
        runlog.check_platform_baseline()
        runlog.bootstrap_runtime_root(app_config.runtime_root)

        clock = _SystemClock()
        sleeper = _SystemSleeper()
        run_id = runlog.generate_run_id(clock)
        process_runner = SubprocessRunner(clock)

        git_executable = git_safety.resolve_git_executable()
        gh_executable = github.resolve_gh_executable()
        opencode_executable = opencode_adapter.resolve_executable()

        git_safety_port = _CliGitSafetyPort(
            process_runner,
            git_executable=git_executable,
            clock=clock,
            utility_timeout_seconds=app_config.execution.utility_timeout_seconds,
            termination_grace_seconds=app_config.execution.termination_grace_seconds,
        )
        issue_resolver = _GhCliIssueResolver(
            process_runner,
            git_executable=git_executable,
            gh_executable=gh_executable,
            github_targets=app_config.github_targets,
            utility_timeout_seconds=app_config.execution.utility_timeout_seconds,
            termination_grace_seconds=app_config.execution.termination_grace_seconds,
        )
        run_store = _CliRunStore(runtime_root=app_config.runtime_root, clock=clock)
        lease_factory = locking.PosixTargetLeaseFactory(
            process_runner,
            git_executable=git_executable,
            clock=clock,
            utility_timeout_seconds=app_config.execution.utility_timeout_seconds,
            termination_grace_seconds=app_config.execution.termination_grace_seconds,
        )
        opencode_preflight = _CliOpenCodePreflightPort(
            process_runner,
            executable=opencode_executable,
            workspace=run_request.workspace,
            target_root=run_request.target_root,
            utility_timeout_seconds=app_config.execution.utility_timeout_seconds,
            termination_grace_seconds=app_config.execution.termination_grace_seconds,
        )
        agent_runner = _CliAgentRunner(
            process_runner,
            executable=opencode_executable,
            target_root=run_request.target_root,
            opencode_timeout_seconds=app_config.execution.opencode_timeout_seconds,
            utility_timeout_seconds=app_config.execution.utility_timeout_seconds,
            termination_grace_seconds=app_config.execution.termination_grace_seconds,
            git_executable=git_executable,
            clock=clock,
        )

        result = run_composed_pipeline(
            run_request=run_request,
            app_config=app_config,
            run_id=run_id,
            process_runner=process_runner,
            clock=clock,
            sleeper=sleeper,
            git_safety_port=git_safety_port,
            issue_resolver=issue_resolver,
            run_store=run_store,
            lease_factory=lease_factory,
            opencode_preflight=opencode_preflight,
            agent_runner=agent_runner,
        )
    except KeyboardInterrupt:
        return _render_preinit_interrupted()
    except OpenCodeToolsError as error:
        return _render_pre_init_failure(error)

    if isinstance(result, IssueResult):
        last_record = run_store.last_record if run_store is not None else None
        return _render_issue_result(result, last_record=last_record)
    return _render_pre_init_failure(result)


__all__ = (
    "build_parser",
    "main",
    "parse_args",
    "run_composed_pipeline",
)

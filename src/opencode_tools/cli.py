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

Rendering `IssueResult`/pre-init errors into the canonical `FINAL_STATUS`
line, stderr summary, and the full exit-code table is a later milestone's
job; `main` returns only a minimal 0-success/1-failure placeholder for now.
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from opencode_tools import __version__, git_safety, github, locking, runlog
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
    RepositoryIdentity,
    ReviewStatus,
    RunOutcome,
    RunRecord,
    RunRequest,
    TargetRepository,
    Workspace,
)
from opencode_tools.errors import LoggingError, OpenCodeToolsError, ProtocolError
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
from opencode_tools.process import SubprocessRunner
from opencode_tools.protocol import parse_agent_response
from opencode_tools.state_machine import classify_attempt_outcome

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


class _SystemSleeper:
    """The real `Sleeper`: an actual blocking `time.sleep`."""

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


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
    def __init__(self, *, runtime_root: Path, clock: Clock) -> None:
        self._runtime_root = runtime_root
        self._clock = clock
        self._run_directory: Path | None = None

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
        try:
            runlog.persist_run_record(record)
        except LoggingError:
            return PersistenceStatus.FAILED
        return PersistenceStatus.OK


# --- OpenCodePreflightPort: caches the digest so a caller that also wants
# to `.verify()` ahead of bootstrap never re-runs the real preflight ------


class _CliOpenCodePreflightPort:
    def __init__(
        self,
        process_runner: ProcessRunner,
        *,
        executable: Path,
        workspace: Workspace,
        utility_timeout_seconds: float,
        termination_grace_seconds: float,
    ) -> None:
        self._process_runner = process_runner
        self._executable = executable
        self._workspace = workspace
        self._utility_timeout_seconds = utility_timeout_seconds
        self._termination_grace_seconds = termination_grace_seconds
        self._digest: str | None = None

    def verify(self) -> str:
        if self._digest is None:
            evidence = opencode_adapter.run_preflight(
                self._process_runner,
                executable=self._executable,
                workspace=self._workspace,
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
        opencode_timeout_seconds: float,
        utility_timeout_seconds: float,
        termination_grace_seconds: float,
    ) -> None:
        self._process_runner = process_runner
        self._executable = executable
        self._opencode_timeout_seconds = opencode_timeout_seconds
        self._utility_timeout_seconds = utility_timeout_seconds
        self._termination_grace_seconds = termination_grace_seconds
        self._issue_locator: IssueLocator | None = None

    def bind_issue_locator(self, issue_locator: IssueLocator) -> None:
        self._issue_locator = issue_locator

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
    ) -> str | None:
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
        except ProtocolError:
            return None
        return evidence.verified_agent

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

        spec = opencode_adapter.build_run_spec(
            self._executable,
            role,
            prompt,
            workspace,
            timeout_seconds=self._opencode_timeout_seconds,
            termination_grace_seconds=self._termination_grace_seconds,
        )
        cycle_component = review_cycle if review_cycle is not None else 0
        capture = opencode_adapter.open_run_capture_sink(
            f"{role.value.lower()}-{cycle_component}-{provider_attempt}-capture"
        )
        process_result = self._process_runner.run(spec, sink=_TeeSink(sink, capture))

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

        if provider_diagnostic is None and not process_result.timed_out:
            transport = self._decode_transport(
                stdout_bytes, overflowed=capture.overflowed("stdout")
            )
            if transport is not None:
                session_id = transport.session_id
                terminal_response = self._parse_response(
                    role, transport.terminal_text, issue_locator=self._issue_locator
                )
                verified_agent = self._verify_identity(role, workspace, session_id)
                if verified_agent is None:
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
        )


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

    if isinstance(agent_runner, _AcceptsIssueLocator):
        agent_runner.bind_issue_locator(outcome.issue_locator)

    target = outcome.record.target
    pipeline_result = run_issue_pipeline(
        orchestrator=outcome.orchestrator,
        issue_locator=outcome.issue_locator,
        workspace=run_request.workspace,
        target=target,
        max_review_cycles=app_config.execution.max_review_cycles,
    )

    return finalize_run(
        record=outcome.orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(pipeline_result),
        review_status=_last_review_status(pipeline_result),
        interrupted=_interrupted(pipeline_result),
        termination_confirmed=_termination_confirmed(pipeline_result),
        git_safety=git_safety_port,
        run_store=run_store,
        lease=outcome.lease,
        clock=clock,
        max_review_cycles=app_config.execution.max_review_cycles,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: validate argv shape, compose adapters, run the pipeline.

    An invalid `argv` is rejected by `argparse` itself with exit code 2,
    before anything else runs. On a syntactically valid parse, this builds
    every real adapter and runs the single-issue pipeline through
    `run_composed_pipeline`. The full canonical exit-code table (0/2/10/20/
    30/40/130) and `FINAL_STATUS`/stderr rendering are a later milestone's
    job; for now this returns only a minimal 0 (approved) / 1 (anything
    else) placeholder.
    """

    args = parse_args(argv)
    cwd = Path.cwd()

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
            utility_timeout_seconds=app_config.execution.utility_timeout_seconds,
            termination_grace_seconds=app_config.execution.termination_grace_seconds,
        )
        agent_runner = _CliAgentRunner(
            process_runner,
            executable=opencode_executable,
            opencode_timeout_seconds=app_config.execution.opencode_timeout_seconds,
            utility_timeout_seconds=app_config.execution.utility_timeout_seconds,
            termination_grace_seconds=app_config.execution.termination_grace_seconds,
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
    except OpenCodeToolsError:
        return 1

    if isinstance(result, IssueResult) and result.final_status is FinalStatus.APPROVED:
        return 0
    return 1


__all__ = (
    "build_parser",
    "main",
    "parse_args",
    "run_composed_pipeline",
)

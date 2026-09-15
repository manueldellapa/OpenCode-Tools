"""Unit tests for the attempt sequence, precedence, persistence, and provider
retry (M12-04).

Exercises `orchestrator.IssueOrchestrator` purely against `Fake`/`Recording`
doubles written directly against `ports.py` `Protocol`s -- no subprocess, no
real clock, no real Git, filesystem, or sleep. Covers M12-02's acceptance
criteria (the observable, invariant order of operations; every failure
still performing its Git check; a higher-precedence outcome never being
replaced while concurrent diagnostics are preserved; Git control-plane/
branch/HEAD drift blocking before the next role), M12-03's (a distinct,
never overwritten `AttemptRecord` persisted after every completed attempt;
a `RunStorePort.persist` failure or a sink-open `LoggingError` blocking
every subsequent invocation; a `process.outcome` of
`LOGGING_ERROR`/`INTERRUPTED` folding into the same non-retryable precedence
as `PROCESS_ERROR`; explicit and process-driven cancellation blocking
subsequent invocations; and the persisted `RunRecord` never carrying the
raw prompt), and M12-04's: `run_provider_attempts`'s guard matrix (a
trusted, budgeted `PROVIDER_ERROR` retries; an untrusted or non-retryable
diagnostic, unconfirmed termination, `UNSAFE` Git, or cancellation does
not); recovery after a transient failure; exhaustion spending exactly the
configured attempt budget without ever touching `review_cycle`; a coder's
own target-fingerprint change suppressing retry while preserving its
mutation; the exact, already-capped backoff delay passed to a fake
`Sleeper`; and a persistence write failure or cancellation preventing both
the sleep and the next attempt. Resume, raw-log reconstruction, retention,
CLI rendering, review rework, and full pipeline composition remain out of
scope (M13).
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    ProviderDiagnostic,
    ProviderRetryConfig,
    RetryDecision,
    ReviewStatus,
    RunOutcome,
    RunRecord,
    TargetRepository,
    Workspace,
    to_primitive,
)
from opencode_tools.errors import LoggingError, ProtocolError, RunInterruptedError
from opencode_tools.orchestrator import (
    InvocationEvent,
    InvocationEventKind,
    IssueOrchestrator,
    LogicalInvocationResult,
)
from opencode_tools.ports import AttemptLogSink, LogChannel
from opencode_tools.state_machine import AttemptPrecedence, PipelineState

_ALLOWED_INTERNAL_MODULES = frozenset(
    {
        "opencode_tools.domain",
        "opencode_tools.errors",
        "opencode_tools.ports",
        "opencode_tools.state_machine",
        "opencode_tools.retry",
        "opencode_tools.prompting",
    }
)
_ORCHESTRATOR_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "opencode_tools" / "orchestrator.py"
)

_PHASE_BY_ROLE: dict[AgentRole, PipelinePhase] = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}

NOW = datetime(2026, 9, 14, 9, 0, 0, tzinfo=UTC)
WORKSPACE_ROOT = Path("/workspaces/opencode-tools")
TARGET_ROOT = WORKSPACE_ROOT / "backend"


def _orchestrator_module_imports() -> frozenset[str]:
    tree = ast.parse(
        _ORCHESTRATOR_SOURCE.read_text(encoding="utf-8"),
        filename=str(_ORCHESTRATOR_SOURCE),
    )
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise AssertionError("orchestrator.py must not use relative imports")
            if node.module is not None:
                modules.add(node.module)
    return frozenset(modules)


def _workspace() -> Workspace:
    return Workspace(root=WORKSPACE_ROOT)


def _target() -> TargetRepository:
    return TargetRepository(
        root=TARGET_ROOT,
        workspace_relative=Path("backend"),
        git_common_dir=TARGET_ROOT / ".git",
    )


def _initial_record(*, run_id: str, target: TargetRepository) -> RunRecord:
    return RunRecord(
        schema_version=1,
        run_id=run_id,
        artifact_path=WORKSPACE_ROOT / ".opencode-tools" / "runs" / run_id / "run.json",
        workspace=_workspace(),
        target=target,
        issue_number=1,
        config={},
        environment={},
        started_at=NOW,
        current_phase=PipelinePhase.PREFLIGHT,
        persistence_status=PersistenceStatus.OK,
    )


def _provider_retry_config() -> ProviderRetryConfig:
    return ProviderRetryConfig(
        max_attempts=3,
        initial_delay_seconds=1.0,
        multiplier=2.0,
        max_delay_seconds=30.0,
    )


def _git_probe_result() -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/git", "status"),
        cwd=TARGET_ROOT,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        duration_ns=1_000_000,
        return_code=0,
        timed_out=False,
        termination_confirmed=True,
        log_path=Path("git-probe.log"),
        stdout_byte_count=0,
        stdout_sha256="git-stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="git-stderr-digest",
        outcome=RunOutcome.SUCCEEDED,
    )


def _git_state(*, fingerprint: str | None = "fingerprint-0") -> GitState:
    return GitState(
        root=TARGET_ROOT,
        branch="main",
        head="deadbeef",
        porcelain_summary="clean",
        staged=(),
        unstaged=(),
        untracked=(),
        fingerprint=fingerprint,
    )


def _git_check(
    *,
    sequence: int,
    purpose: str,
    safety_status: GitSafetyStatus = GitSafetyStatus.SAFE,
    state: GitState | None = None,
    compared_to: str | None = None,
) -> GitCheckRecord:
    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=(_git_probe_result(),),
        state=state if state is not None else _git_state(),
        safety_status=safety_status,
        compared_to=compared_to,
    )


def _agent_process_result(
    *,
    outcome: RunOutcome = RunOutcome.SUCCEEDED,
    timed_out: bool = False,
    return_code: int | None = 0,
    termination_confirmed: bool | None = True,
) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/opencode", "run"),
        cwd=WORKSPACE_ROOT,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=5),
        duration_ns=5_000_000_000,
        return_code=return_code,
        timed_out=timed_out,
        termination_confirmed=termination_confirmed,
        log_path=Path("attempt.log"),
        stdout_byte_count=0,
        stdout_sha256="stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="stderr-digest",
        outcome=outcome,
    )


def _success_response(role: AgentRole) -> ParsedAgentResponse:
    if role is AgentRole.ARCHITECT:
        return ParsedAgentResponse(
            role=role, body="ready", agent_status=AgentStatus.READY
        )
    if role is AgentRole.CODER:
        return ParsedAgentResponse(
            role=role, body="done", agent_status=AgentStatus.COMPLETED
        )
    return ParsedAgentResponse(
        role=role, body="approved", review_status=ReviewStatus.APPROVED
    )


def _failure_response(role: AgentRole) -> ParsedAgentResponse:
    return ParsedAgentResponse(
        role=role, body="could not complete", agent_status=AgentStatus.FAILED
    )


def _provider_diagnostic(*, retryable: bool = True) -> ProviderDiagnostic:
    return ProviderDiagnostic(
        source="opencode-stdout",
        signature="rate_limited",
        retryable=retryable,
        status_code=429,
    )


def _agent_result(
    *,
    role: AgentRole,
    review_cycle: int | None,
    provider_attempt: int,
    process: ProcessResult | None = None,
    terminal_response: ParsedAgentResponse | None = None,
    provider_diagnostic: ProviderDiagnostic | None = None,
    outcome: RunOutcome = RunOutcome.SUCCEEDED,
) -> AgentResult:
    return AgentResult(
        role=role,
        phase=_PHASE_BY_ROLE[role],
        review_cycle=review_cycle,
        provider_attempt=provider_attempt,
        process=process if process is not None else _agent_process_result(),
        terminal_response=terminal_response,
        session_id="session-001",
        verified_agent=role.value.lower(),
        provider_diagnostic=provider_diagnostic,
        outcome=outcome,
    )


def _pipeline_state(*, role: AgentRole, review_cycle: int | None) -> PipelineState:
    return PipelineState(phase=_PHASE_BY_ROLE[role], review_cycle=review_cycle)


def _no_retry_decision() -> RetryDecision:
    return RetryDecision(
        should_retry=False,
        next_provider_attempt=None,
        planned_delay_seconds=None,
        retry_suppressed_due_to_target_change=False,
    )


class RecordingAttemptLogSink:
    """A minimal `AttemptLogSink` fake that records writes and closing."""

    def __init__(self, path: Path = Path("attempt.log")) -> None:
        self._path = path
        self.writes: list[tuple[LogChannel, bytes, datetime]] = []
        self.closed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        self.writes.append((channel, payload, timestamp))

    def close(self) -> None:
        self.closed = True


class RecordingRunStorePort:
    """A `RunStorePort` fake that appends to a shared cross-port call log."""

    def __init__(
        self,
        call_log: list[str],
        *,
        sink: RecordingAttemptLogSink,
        persist_results: list[PersistenceStatus] | None = None,
        fail_open: bool = False,
    ) -> None:
        self._call_log = call_log
        self._sink = sink
        self._persist_results = (
            list(persist_results) if persist_results is not None else None
        )
        self._fail_open = fail_open
        self.sink_calls: list[tuple[AgentRole, int | None, int]] = []
        self.persist_calls: list[RunRecord] = []

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        raise AssertionError("bootstrap concern, not exercised here")

    def open_attempt_sink(
        self,
        role: AgentRole,
        review_cycle: int | None,
        provider_attempt: int,
    ) -> AttemptLogSink:
        self._call_log.append("run_store.open_attempt_sink")
        if self._fail_open:
            raise LoggingError(
                code="runlog.attempt_sink_collision",
                message="Could not open an exclusive attempt sink.",
            )
        self.sink_calls.append((role, review_cycle, provider_attempt))
        return self._sink

    def persist(self, record: RunRecord) -> PersistenceStatus:
        self._call_log.append("run_store.persist")
        self.persist_calls.append(record)
        if self._persist_results is not None:
            return self._persist_results.pop(0)
        return PersistenceStatus.OK


class ScriptedAgentRunner:
    """An `AgentRunner` fake that appends to a shared cross-port call log."""

    def __init__(self, call_log: list[str], *, result: AgentResult) -> None:
        self._call_log = call_log
        self._result = result
        self.calls: list[
            tuple[AgentRole, str, Workspace, int | None, int, AttemptLogSink]
        ] = []

    def queue_result(self, result: AgentResult) -> None:
        """Replace the canned result returned by the next `run()` call."""
        self._result = result

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
        self._call_log.append("agent_runner.run")
        self.calls.append(
            (role, prompt, workspace, review_cycle, provider_attempt, sink)
        )
        return self._result


class QueuedAgentRunner:
    """An `AgentRunner` fake returning one scripted result per call, in order.

    Unlike `ScriptedAgentRunner`, whose single `_result` only changes when a
    test explicitly calls `queue_result` between two of its own calls, this
    fake is for `IssueOrchestrator.run_provider_attempts`'s own internal
    multi-attempt loop, where no test code runs between calls to inject the
    next result.
    """

    def __init__(self, results: list[AgentResult]) -> None:
        self._results = list(results)
        self.calls: list[
            tuple[AgentRole, str, Workspace, int | None, int, AttemptLogSink]
        ] = []

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
        self.calls.append(
            (role, prompt, workspace, review_cycle, provider_attempt, sink)
        )
        return self._results.pop(0)


class CancellingAgentRunner:
    """An `AgentRunner` fake that requests cancellation as a side effect.

    Simulates a SIGINT/SIGTERM observed *during* an attempt whose own child
    process nonetheless reports a normal (non-`INTERRUPTED`) outcome -- the
    race the "nel sleep" acceptance case guards against: cancellation must
    suppress a subsequent retry even when it did not originate from this
    attempt's own process outcome. `on_run` is a zero-argument callback
    (typically `orchestrator.request_cancellation`) rather than the
    orchestrator itself, since the orchestrator cannot exist yet when this
    fake is constructed as one of its own dependencies.
    """

    def __init__(self, *, result: AgentResult, on_run: Callable[[], None]) -> None:
        self._result = result
        self._on_run = on_run
        self.calls = 0

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
        self.calls += 1
        self._on_run()
        return self._result


class RecordingGitSafetyPort:
    """A `GitSafetyPort` fake returning one scripted check result per call."""

    def __init__(self, call_log: list[str], results: list[GitCheckRecord]) -> None:
        self._call_log = call_log
        self._results = list(results)
        self.calls: list[
            tuple[TargetRepository, int, str, AgentRole | None, GitState | None]
        ] = []

    def check_runtime_location(self, runtime_root: Path) -> None:
        raise AssertionError("not exercised by this issue's orchestrator scope")

    def resolve_target(
        self, workspace: Workspace, target_root: Path
    ) -> TargetRepository:
        raise AssertionError("not exercised by this issue's orchestrator scope")

    def check(
        self,
        target: TargetRepository,
        *,
        sequence: int,
        purpose: str,
        role: AgentRole | None = None,
        baseline: GitState | None = None,
    ) -> GitCheckRecord:
        self._call_log.append("git_safety.check")
        self.calls.append((target, sequence, purpose, role, baseline))
        return self._results.pop(0)


CONTROL_PLANE_DIGEST = "control-plane-digest-abc123"


class RecordingOpenCodePreflightPort:
    """An `OpenCodePreflightPort` fake that appends to a shared cross-port
    call log. `verify()` is never exercised here -- `bootstrap_run` (M13-01)
    calls it, not `IssueOrchestrator` -- only `recheck`, once per provider
    attempt, scriptable to raise a drift on a specific call."""

    def __init__(
        self,
        call_log: list[str],
        *,
        recheck_errors: list[Exception | None] | None = None,
    ) -> None:
        self._call_log = call_log
        self._recheck_errors = (
            list(recheck_errors) if recheck_errors is not None else None
        )
        self.recheck_calls: list[str] = []

    def verify(self) -> str:
        raise AssertionError("not exercised by this issue's orchestrator scope")

    def recheck(self, expected_digest: str) -> None:
        self._call_log.append("opencode_preflight.recheck")
        self.recheck_calls.append(expected_digest)
        if self._recheck_errors:
            error = self._recheck_errors.pop(0)
            if error is not None:
                raise error


class SteppingClock:
    """A `Clock` fake whose `now()` advances by one second on every call."""

    def __init__(self, *, start: datetime) -> None:
        self._next = start
        self.calls = 0

    def now(self) -> datetime:
        current = self._next
        self._next = current + timedelta(seconds=1)
        self.calls += 1
        return current

    def monotonic_ns(self) -> int:
        raise AssertionError("not exercised by this issue's orchestrator scope")


class RecordingSleeper:
    """A `Sleeper` fake that records requested delays without ever sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


def _orchestrator(
    *,
    call_log: list[str],
    sink: RecordingAttemptLogSink,
    agent_result: AgentResult,
    git_results: list[GitCheckRecord],
    clock: SteppingClock,
    run_id: str = "run-001",
    target: TargetRepository | None = None,
    persist_results: list[PersistenceStatus] | None = None,
    fail_open: bool = False,
    sleeper: RecordingSleeper | None = None,
    provider_retry: ProviderRetryConfig | None = None,
    recheck_errors: list[Exception | None] | None = None,
) -> tuple[
    IssueOrchestrator,
    RecordingRunStorePort,
    ScriptedAgentRunner,
    RecordingGitSafetyPort,
    RecordingOpenCodePreflightPort,
]:
    resolved_target = target if target is not None else _target()
    run_store = RecordingRunStorePort(
        call_log, sink=sink, persist_results=persist_results, fail_open=fail_open
    )
    agent_runner = ScriptedAgentRunner(call_log, result=agent_result)
    git_safety = RecordingGitSafetyPort(call_log, git_results)
    opencode_preflight = RecordingOpenCodePreflightPort(
        call_log, recheck_errors=recheck_errors
    )
    orchestrator = IssueOrchestrator(
        initial_record=_initial_record(run_id=run_id, target=resolved_target),
        agent_runner=agent_runner,
        run_store=run_store,
        git_safety=git_safety,
        opencode_preflight=opencode_preflight,
        control_plane_digest=CONTROL_PLANE_DIGEST,
        clock=clock,
        sleeper=sleeper if sleeper is not None else RecordingSleeper(),
        provider_retry=(
            provider_retry if provider_retry is not None else _provider_retry_config()
        ),
    )
    return orchestrator, run_store, agent_runner, git_safety, opencode_preflight


def _safe_pair(*, attempt_fingerprint: str = "fingerprint-1") -> list[GitCheckRecord]:
    before = _git_check(sequence=0, purpose="before", state=_git_state())
    after = _git_check(
        sequence=1,
        purpose="after",
        state=_git_state(fingerprint=attempt_fingerprint),
        compared_to="before",
    )
    return [before, after]


def _safe_pair_unchanged(
    *, fingerprint: str = "fingerprint-unchanged"
) -> list[GitCheckRecord]:
    """A (before, after) pair with a matching fingerprint -- i.e. Git safety
    is `SAFE` but the target did *not* change, isolating `decide_retry`'s
    other guards from its coder-specific `target_changed` suppression."""

    before = _git_check(
        sequence=0, purpose="before", state=_git_state(fingerprint=fingerprint)
    )
    after = _git_check(
        sequence=1,
        purpose="after",
        state=_git_state(fingerprint=fingerprint),
        compared_to="before",
    )
    return [before, after]


# --- Import boundary --------------------------------------------------------


def test_orchestrator_module_imports_only_domain_ports_and_pure_policy() -> None:
    imports = _orchestrator_module_imports()

    internal = {name for name in imports if name.startswith("opencode_tools")}
    assert internal, "orchestrator.py must depend on at least one internal module"
    assert internal <= _ALLOWED_INTERNAL_MODULES


# --- Dependency injection ----------------------------------------------------


def test_orchestrator_is_constructed_entirely_from_injected_ports() -> None:
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    result = _agent_result(
        role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
    )
    clock = SteppingClock(start=NOW)

    orchestrator, run_store, agent_runner, git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=sink,
            agent_result=result,
            git_results=_safe_pair(),
            clock=clock,
        )
    )

    assert isinstance(orchestrator, IssueOrchestrator)
    assert run_store.sink_calls == []
    assert run_store.persist_calls == []
    assert agent_runner.calls == []
    assert git_safety.calls == []


def test_orchestrator_rejects_empty_run_id() -> None:
    with pytest.raises(ValueError, match="run_id"):
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
            run_id="",
        )


def test_orchestrator_rejects_a_target_of_the_wrong_type() -> None:
    with pytest.raises(TypeError, match="target"):
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
            target=cast(TargetRepository, TARGET_ROOT),
        )


def test_orchestrator_rejects_an_initial_record_of_the_wrong_type() -> None:
    with pytest.raises(TypeError, match="initial_record"):
        IssueOrchestrator(
            initial_record=cast(RunRecord, {"not": "a run record"}),
            agent_runner=ScriptedAgentRunner(
                [],
                result=_agent_result(
                    role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
                ),
            ),
            run_store=RecordingRunStorePort([], sink=RecordingAttemptLogSink()),
            git_safety=RecordingGitSafetyPort([], []),
            opencode_preflight=RecordingOpenCodePreflightPort([]),
            control_plane_digest=CONTROL_PLANE_DIGEST,
            clock=SteppingClock(start=NOW),
            sleeper=RecordingSleeper(),
            provider_retry=_provider_retry_config(),
        )


def test_orchestrator_rejects_a_provider_retry_of_the_wrong_type() -> None:
    with pytest.raises(TypeError, match="provider_retry"):
        IssueOrchestrator(
            initial_record=_initial_record(run_id="run-001", target=_target()),
            agent_runner=ScriptedAgentRunner(
                [],
                result=_agent_result(
                    role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
                ),
            ),
            run_store=RecordingRunStorePort([], sink=RecordingAttemptLogSink()),
            git_safety=RecordingGitSafetyPort([], []),
            opencode_preflight=RecordingOpenCodePreflightPort([]),
            control_plane_digest=CONTROL_PLANE_DIGEST,
            clock=SteppingClock(start=NOW),
            sleeper=RecordingSleeper(),
            provider_retry=cast(ProviderRetryConfig, object()),
        )


# --- Order of operations and call recording ---------------------------------


def test_run_logical_invocation_follows_the_canonical_order_when_safe() -> None:
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=2,
        provider_attempt=1,
        terminal_response=_success_response(AgentRole.CODER),
    )
    clock = SteppingClock(start=NOW)
    target = _target()
    git_results = _safe_pair()
    orchestrator, run_store, agent_runner, git_safety, opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=sink,
            agent_result=agent_result,
            git_results=git_results,
            clock=clock,
            target=target,
        )
    )
    workspace = _workspace()

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=2,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    assert call_log == [
        "run_store.open_attempt_sink",
        "git_safety.check",
        "opencode_preflight.recheck",
        "agent_runner.run",
        "git_safety.check",
        "run_store.persist",
    ]
    assert run_store.sink_calls == [(AgentRole.CODER, 2, 1)]
    assert agent_runner.calls == [
        (AgentRole.CODER, "Implement the fix.", workspace, 2, 1, sink)
    ]
    assert sink.closed is True
    assert [(call[0], call[3], call[4]) for call in git_safety.calls] == [
        (target, None, None),
        (target, AgentRole.CODER, git_results[0].state),
    ]
    assert result.git_before is git_results[0]
    assert result.control_plane_error is None
    assert opencode_preflight.recheck_calls == [CONTROL_PLANE_DIGEST]
    assert result.git_after is git_results[1]
    assert result.agent_result is agent_result
    assert result.retry_decision is not None
    assert result.retry_decision.should_retry is False
    assert [event.kind for event in result.events] == [
        InvocationEventKind.INVOCATION_STARTED,
        InvocationEventKind.ATTEMPT_SINK_OPENED,
        InvocationEventKind.GIT_BEFORE_CHECKED,
        InvocationEventKind.CONTROL_PLANE_RECHECKED,
        InvocationEventKind.AGENT_RESULT_RECEIVED,
        InvocationEventKind.GIT_AFTER_CHECKED,
        InvocationEventKind.ATTEMPT_SINK_CLOSED,
    ]
    assert [event.sequence for event in result.events] == [0, 1, 2, 3, 4, 5, 6]
    timestamps = [event.timestamp for event in result.events]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == 7

    # Exactly one persist call, carrying a distinct AttemptRecord for it.
    assert len(run_store.persist_calls) == 1
    persisted = run_store.persist_calls[0]
    assert len(persisted.attempts) == 1
    attempt = persisted.attempts[0]
    assert attempt.role is AgentRole.CODER
    assert attempt.review_cycle == 2
    assert attempt.provider_attempt == 1
    assert attempt.agent_result is agent_result
    assert attempt.retry_decision is False
    assert persisted.current_phase is PipelinePhase.CODER
    assert persisted.review_cycle == 2
    assert persisted.provider_attempt == 1


def test_run_logical_invocation_before_check_passes_role_none_and_after_passes_role() -> (
    None
):
    call_log: list[str] = []
    orchestrator, _run_store, _agent_runner, git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                provider_attempt=1,
                terminal_response=_success_response(AgentRole.ARCHITECT),
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        prompt="Design the fix.",
        workspace=_workspace(),
    )

    before_call, after_call = git_safety.calls
    assert before_call[3] is None
    assert after_call[3] is AgentRole.ARCHITECT


# --- Git continuity across invocations ---------------------------------------


def test_run_logical_invocation_before_baseline_is_the_last_accepted_after_state() -> (
    None
):
    call_log: list[str] = []
    clock = SteppingClock(start=NOW)
    workspace = _workspace()
    first_pair = _safe_pair(attempt_fingerprint="fingerprint-after-1")
    second_pair = [
        _git_check(sequence=2, purpose="before-2", state=first_pair[1].state),
        _git_check(
            sequence=3,
            purpose="after-2",
            state=_git_state(fingerprint="fingerprint-after-2"),
        ),
    ]
    orchestrator, _run_store, agent_runner, git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                terminal_response=_success_response(AgentRole.CODER),
            ),
            git_results=[*first_pair, *second_pair],
            clock=clock,
        )
    )

    orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )
    agent_runner.queue_result(
        _agent_result(
            role=AgentRole.REVIEWER,
            review_cycle=1,
            provider_attempt=1,
            terminal_response=_success_response(AgentRole.REVIEWER),
        )
    )
    orchestrator.run_logical_invocation(
        role=AgentRole.REVIEWER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Review the fix.",
        workspace=workspace,
    )

    assert git_safety.calls[0][4] is None  # very first before: no baseline yet
    assert git_safety.calls[2][4] is first_pair[1].state  # continuous with attempt 1


def test_run_logical_invocation_does_not_accept_an_unsafe_after_as_the_new_baseline() -> (
    None
):
    call_log: list[str] = []
    workspace = _workspace()
    drifted_after = _git_check(
        sequence=1,
        purpose="after-1",
        safety_status=GitSafetyStatus.UNSAFE,
        state=_git_state(fingerprint="fingerprint-drift"),
    )
    before_1 = _git_check(sequence=0, purpose="before-1")
    before_2 = _git_check(sequence=2, purpose="before-2")
    after_2 = _git_check(
        sequence=3, purpose="after-2", state=_git_state(fingerprint="fingerprint-2")
    )
    orchestrator, _run_store, agent_runner, git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                provider_attempt=1,
                terminal_response=_success_response(AgentRole.ARCHITECT),
            ),
            git_results=[before_1, drifted_after, before_2, after_2],
            clock=SteppingClock(start=NOW),
        )
    )

    orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        prompt="Design the fix.",
        workspace=workspace,
    )
    agent_runner.queue_result(
        _agent_result(
            role=AgentRole.ARCHITECT,
            review_cycle=None,
            provider_attempt=2,
            terminal_response=_success_response(AgentRole.ARCHITECT),
        )
    )
    orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=2,
        prompt="Design the fix, retried.",
        workspace=workspace,
    )

    # The drifted (unsafe) after-check never becomes the trusted baseline.
    assert git_safety.calls[2][4] is None


# --- Git safety blocks the agent before it is ever invoked -------------------


@pytest.mark.parametrize(
    "before_status", [GitSafetyStatus.UNSAFE, GitSafetyStatus.INDETERMINATE]
)
def test_run_logical_invocation_blocks_the_agent_when_before_is_not_safe(
    before_status: GitSafetyStatus,
) -> None:
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    before = _git_check(sequence=0, purpose="before", safety_status=before_status)
    orchestrator, run_store, agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=sink,
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_results=[before],
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert call_log == ["run_store.open_attempt_sink", "git_safety.check"]
    assert run_store.sink_calls == [(AgentRole.CODER, 1, 1)]
    assert run_store.persist_calls == []
    assert agent_runner.calls == []
    assert sink.closed is True
    assert result.git_before is before
    assert result.agent_result is None
    assert result.git_after is None
    assert result.precedence is None
    assert result.retry_decision is None
    assert [event.kind for event in result.events] == [
        InvocationEventKind.INVOCATION_STARTED,
        InvocationEventKind.ATTEMPT_SINK_OPENED,
        InvocationEventKind.GIT_BEFORE_CHECKED,
        InvocationEventKind.ATTEMPT_SINK_CLOSED,
        InvocationEventKind.BLOCKED_BY_GIT_SAFETY,
    ]


# --- Control-plane drift blocks the agent before it is ever invoked ---------


def test_run_logical_invocation_blocks_the_agent_on_a_control_plane_drift() -> None:
    """A `before` that is `SAFE` still never reaches `AgentRunner.run()` if
    `OpenCodePreflightPort.recheck` detects a control-plane drift (System
    Design SS18.2; ADR-005; the M12-02/#45 gap this closes): the recheck
    runs after `before` is confirmed `SAFE` and strictly before the agent is
    invoked, and a drift fails closed exactly like an unsafe `before` does
    -- no agent call, no `git_after`, no precedence, no retry decision."""

    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    before = _git_check(sequence=0, purpose="before")
    drift = ProtocolError(
        "opencode.control_plane_drift", "the control-plane digest changed"
    )
    orchestrator, run_store, agent_runner, _git_safety, opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=sink,
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_results=[before],
            clock=SteppingClock(start=NOW),
            recheck_errors=[drift],
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert call_log == [
        "run_store.open_attempt_sink",
        "git_safety.check",
        "opencode_preflight.recheck",
    ]
    assert opencode_preflight.recheck_calls == [CONTROL_PLANE_DIGEST]
    assert run_store.sink_calls == [(AgentRole.CODER, 1, 1)]
    assert run_store.persist_calls == []  # never persisted as an AttemptRecord
    assert agent_runner.calls == []  # the agent is never invoked
    assert sink.closed is True
    assert result.git_before is before
    assert result.control_plane_error is drift
    assert result.agent_result is None
    assert result.git_after is None
    assert result.precedence is None
    assert result.retry_decision is None
    assert [event.kind for event in result.events] == [
        InvocationEventKind.INVOCATION_STARTED,
        InvocationEventKind.ATTEMPT_SINK_OPENED,
        InvocationEventKind.GIT_BEFORE_CHECKED,
        InvocationEventKind.ATTEMPT_SINK_CLOSED,
        InvocationEventKind.BLOCKED_BY_CONTROL_PLANE_DRIFT,
    ]


def test_run_provider_attempts_stops_after_a_control_plane_drift_without_retrying() -> (
    None
):
    """`retry_decision is None` on a drift-blocked attempt (mirroring the
    git-safety-blocked case) means `run_provider_attempts` stops immediately
    -- a drift is never retried, and the agent is never invoked at all."""

    call_log: list[str] = []
    before = _git_check(sequence=0, purpose="before")
    drift = ProtocolError(
        "opencode.control_plane_drift", "the control-plane digest changed"
    )
    sleeper = RecordingSleeper()
    orchestrator, _run_store, agent_runner, _git_safety, opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_results=[before],
            clock=SteppingClock(start=NOW),
            sleeper=sleeper,
            recheck_errors=[drift],
        )
    )

    results = orchestrator.run_provider_attempts(
        role=AgentRole.CODER,
        review_cycle=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert len(results) == 1
    assert results[0].control_plane_error is drift
    assert results[0].retry_decision is None
    assert agent_runner.calls == []
    assert sleeper.calls == []
    assert opencode_preflight.recheck_calls == [CONTROL_PLANE_DIGEST]


# --- ID / counter invariants -------------------------------------------------


def test_run_logical_invocation_assigns_the_same_identity_across_provider_attempts() -> (
    None
):
    call_log: list[str] = []
    workspace = _workspace()
    orchestrator, _run_store, agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                outcome=RunOutcome.PROVIDER_ERROR,
                provider_diagnostic=_provider_diagnostic(),
            ),
            git_results=[*_safe_pair_unchanged(), *_safe_pair()],
            clock=SteppingClock(start=NOW),
        )
    )

    first = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    agent_runner.queue_result(
        _agent_result(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=2,
            terminal_response=_success_response(AgentRole.CODER),
        )
    )
    second = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=2,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    assert first.logical_invocation_id == second.logical_invocation_id
    assert first.provider_attempt == 1
    assert second.provider_attempt == 2
    assert first.retry_decision is not None
    assert first.retry_decision.should_retry is True  # retryable, budget left
    assert second.retry_decision is not None
    assert second.retry_decision.should_retry is False  # a clean success


def test_run_logical_invocation_rejects_architect_with_a_review_cycle() -> None:
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    with pytest.raises(ValueError, match="review_cycle"):
        orchestrator.run_logical_invocation(
            role=AgentRole.ARCHITECT,
            review_cycle=1,
            provider_attempt=1,
            prompt="prompt",
            workspace=_workspace(),
        )


def test_run_logical_invocation_rejects_coder_without_a_review_cycle() -> None:
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    with pytest.raises(ValueError, match="review_cycle"):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=None,
            provider_attempt=1,
            prompt="prompt",
            workspace=_workspace(),
        )


def test_run_logical_invocation_rejects_non_positive_provider_attempt() -> None:
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    with pytest.raises(ValueError, match="provider_attempt"):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=0,
            prompt="prompt",
            workspace=_workspace(),
        )


def test_run_logical_invocation_rejects_wrong_types() -> None:
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    with pytest.raises(TypeError, match="role"):
        orchestrator.run_logical_invocation(
            role=cast(AgentRole, "CODER"),
            review_cycle=1,
            provider_attempt=1,
            prompt="prompt",
            workspace=_workspace(),
        )
    with pytest.raises(TypeError, match="workspace"):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=1,
            prompt="prompt",
            workspace=cast(Workspace, WORKSPACE_ROOT),
        )


# --- Exhaustive technical precedence -----------------------------------------


def test_precedence_classifies_a_pure_timeout() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(
            outcome=RunOutcome.TIMEOUT, timed_out=True, return_code=None
        ),
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.TIMEOUT, concurrent_signals=(RunOutcome.TIMEOUT,)
    )


def test_precedence_classifies_a_pure_provider_error() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        provider_diagnostic=_provider_diagnostic(),
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROVIDER_ERROR,
        concurrent_signals=(RunOutcome.PROVIDER_ERROR,),
    )


def test_precedence_classifies_a_pure_process_error() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(outcome=RunOutcome.PROCESS_ERROR, return_code=1),
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROCESS_ERROR,
        concurrent_signals=(RunOutcome.PROCESS_ERROR,),
    )


def test_precedence_classifies_a_logging_error_process_outcome_as_process_error() -> (
    None
):
    """M12-03: SS13.2 has no dedicated `LOGGING_ERROR` attempt category, so a
    sink write fault (the child already terminated by `ProcessRunner`, per
    System Design SS15.5) folds into the same non-retryable `PROCESS_ERROR`
    precedence -- while the true outcome stays on `agent_result.process`."""

    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(
            outcome=RunOutcome.LOGGING_ERROR, return_code=None
        ),
    )
    orchestrator, run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROCESS_ERROR,
        concurrent_signals=(RunOutcome.PROCESS_ERROR,),
    )
    assert result.agent_result is not None
    assert result.agent_result.process.outcome is RunOutcome.LOGGING_ERROR
    assert len(run_store.persist_calls) == 1  # still persisted once, distinctly


def test_precedence_classifies_an_interrupted_process_outcome_as_process_error() -> (
    None
):
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(outcome=RunOutcome.INTERRUPTED, return_code=None),
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROCESS_ERROR,
        concurrent_signals=(RunOutcome.PROCESS_ERROR,),
    )
    assert result.agent_result is not None
    assert result.agent_result.process.outcome is RunOutcome.INTERRUPTED


def test_precedence_classifies_a_pure_protocol_error() -> None:
    """Also stands in for a control-plane digest drift (System Design line
    1116): the technical process succeeded, but no parseable terminal
    response is available, which classifies identically -- `PROTOCOL_ERROR`
    -- and blocks the caller from proceeding to the next role.
    """

    agent_result = _agent_result(
        role=AgentRole.CODER, review_cycle=1, provider_attempt=1, terminal_response=None
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROTOCOL_ERROR,
        concurrent_signals=(RunOutcome.PROTOCOL_ERROR,),
    )


def test_precedence_classifies_a_pure_agent_reported_failure() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        terminal_response=_failure_response(AgentRole.CODER),
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.AGENT_REPORTED_FAILURE,
        concurrent_signals=(RunOutcome.AGENT_REPORTED_FAILURE,),
    )


def test_precedence_classifies_a_pure_success() -> None:
    agent_result = _agent_result(
        role=AgentRole.REVIEWER,
        review_cycle=1,
        provider_attempt=1,
        terminal_response=_success_response(AgentRole.REVIEWER),
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.REVIEWER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
    )


def test_precedence_keeps_timeout_over_a_concurrent_provider_signal() -> None:
    """A timeout with a provider-error-like signal in the partial output:
    `TIMEOUT` wins (higher precedence) but the provider signal is preserved
    as a concurrent cause, never silently discarded (AC-031)."""

    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(
            outcome=RunOutcome.TIMEOUT, timed_out=True, return_code=None
        ),
        provider_diagnostic=_provider_diagnostic(),
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.TIMEOUT,
        concurrent_signals=(RunOutcome.TIMEOUT, RunOutcome.PROVIDER_ERROR),
    )


def test_precedence_keeps_provider_error_over_a_concurrent_process_error() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(outcome=RunOutcome.PROCESS_ERROR, return_code=1),
        provider_diagnostic=_provider_diagnostic(),
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROVIDER_ERROR,
        concurrent_signals=(RunOutcome.PROVIDER_ERROR, RunOutcome.PROCESS_ERROR),
    )


def test_precedence_never_reads_terminal_response_once_timeout_forbids_it() -> None:
    """Even if a `terminal_response` is (adversarially) present on a timed
    out attempt, it must never surface as `SUCCEEDED` or otherwise change
    the outcome -- the parser's semantics are never consulted once a
    higher-precedence technical signal already applies."""

    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(
            outcome=RunOutcome.TIMEOUT, timed_out=True, return_code=None
        ),
        terminal_response=_success_response(AgentRole.CODER),
    )
    orchestrator, _run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.TIMEOUT, concurrent_signals=(RunOutcome.TIMEOUT,)
    )


# --- Attempt persistence: distinct records, never overwritten (M12-03) ------


def test_multiple_attempts_accumulate_as_distinct_records_never_overwritten() -> None:
    call_log: list[str] = []
    workspace = _workspace()
    orchestrator, run_store, agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                provider_attempt=1,
                terminal_response=_success_response(AgentRole.ARCHITECT),
            ),
            git_results=[*_safe_pair(), *_safe_pair()],
            clock=SteppingClock(start=NOW),
        )
    )

    orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        prompt="Design the fix.",
        workspace=workspace,
    )
    agent_runner.queue_result(
        _agent_result(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=1,
            terminal_response=_success_response(AgentRole.CODER),
        )
    )
    orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    assert len(run_store.persist_calls) == 2
    first_persisted, second_persisted = run_store.persist_calls
    assert len(first_persisted.attempts) == 1
    assert len(second_persisted.attempts) == 2
    # The architect's attempt record is carried forward untouched, not
    # replaced, by the second, larger snapshot.
    assert second_persisted.attempts[0] == first_persisted.attempts[0]
    assert second_persisted.attempts[0].role is AgentRole.ARCHITECT
    assert second_persisted.attempts[1].role is AgentRole.CODER


def test_run_logical_invocation_never_persists_the_raw_prompt() -> None:
    secret_prompt = "SECRET-PROMPT-CONTENTS-42"
    orchestrator, run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                terminal_response=_success_response(AgentRole.CODER),
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt=secret_prompt,
        workspace=_workspace(),
    )

    assert len(run_store.persist_calls) == 1
    serialized = str(to_primitive(run_store.persist_calls[0]))
    assert secret_prompt not in serialized


# --- Persistence failure blocks new invocations (M12-03) --------------------


def test_a_persist_failure_raises_and_blocks_the_next_invocation() -> None:
    call_log: list[str] = []
    orchestrator, run_store, agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=call_log,
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                terminal_response=_success_response(AgentRole.CODER),
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
            persist_results=[PersistenceStatus.FAILED],
        )
    )

    with pytest.raises(LoggingError, match="persist"):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=1,
            prompt="Implement the fix.",
            workspace=_workspace(),
        )

    assert len(run_store.persist_calls) == 1

    with pytest.raises(LoggingError, match="persistence"):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=2,
            prompt="Implement the fix, retried.",
            workspace=_workspace(),
        )

    # The second call never opened a new sink, never touched Git, and never
    # attempted a second persist: it failed fast, before anything else.
    assert run_store.sink_calls == [(AgentRole.CODER, 1, 1)]
    assert len(agent_runner.calls) == 1
    assert len(run_store.persist_calls) == 1


def test_a_sink_open_failure_raises_and_blocks_the_next_invocation() -> None:
    orchestrator, _run_store, agent_runner, git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_results=[],
            clock=SteppingClock(start=NOW),
            fail_open=True,
        )
    )

    with pytest.raises(LoggingError):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=1,
            prompt="Implement the fix.",
            workspace=_workspace(),
        )

    assert agent_runner.calls == []
    assert git_safety.calls == []

    with pytest.raises(LoggingError, match="persistence"):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=2,
            prompt="Implement the fix, retried.",
            workspace=_workspace(),
        )


# --- Cancellation blocks new invocations (M12-03, SH-001) --------------------


def test_request_cancellation_blocks_a_subsequent_invocation_before_it_starts() -> None:
    """The "prima" (idle) cancellation case: no port is touched at all."""

    orchestrator, run_store, agent_runner, git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_results=[],
            clock=SteppingClock(start=NOW),
        )
    )

    orchestrator.request_cancellation()

    with pytest.raises(RunInterruptedError):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=1,
            prompt="Implement the fix.",
            workspace=_workspace(),
        )

    assert run_store.sink_calls == []
    assert run_store.persist_calls == []
    assert agent_runner.calls == []
    assert git_safety.calls == []


def test_an_interrupted_process_outcome_blocks_the_next_invocation() -> None:
    """The "durante" (active) cancellation case: the child itself reports
    `INTERRUPTED`; this attempt still completes and is still persisted, but
    every later attempt is blocked."""

    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(outcome=RunOutcome.INTERRUPTED, return_code=None),
    )
    orchestrator, run_store, _agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=agent_result,
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
        )
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )
    assert result.agent_result is agent_result
    assert len(run_store.persist_calls) == 1

    with pytest.raises(RunInterruptedError):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=2,
            prompt="Implement the fix, retried.",
            workspace=_workspace(),
        )


def test_cancellation_requested_during_the_attempt_suppresses_an_otherwise_authorized_retry() -> (
    None
):
    """The "nel sleep" case: cancellation observed mid-attempt (but not via
    this attempt's own process outcome) must still suppress a retry that
    would otherwise be authorized -- so no backoff sleep is ever scheduled
    for it, without this module implementing any sleep loop itself."""

    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        outcome=RunOutcome.PROVIDER_ERROR,
        provider_diagnostic=_provider_diagnostic(retryable=True),
    )
    run_store = RecordingRunStorePort(call_log, sink=sink)
    git_safety = RecordingGitSafetyPort(call_log, _safe_pair_unchanged())
    orchestrator_box: list[IssueOrchestrator] = []
    cancelling_runner = CancellingAgentRunner(
        result=agent_result,
        on_run=lambda: orchestrator_box[0].request_cancellation(),
    )
    orchestrator = IssueOrchestrator(
        initial_record=_initial_record(run_id="run-001", target=_target()),
        agent_runner=cancelling_runner,
        run_store=run_store,
        git_safety=git_safety,
        opencode_preflight=RecordingOpenCodePreflightPort(call_log),
        control_plane_digest=CONTROL_PLANE_DIGEST,
        clock=SteppingClock(start=NOW),
        sleeper=RecordingSleeper(),
        provider_retry=_provider_retry_config(),
    )
    orchestrator_box.append(orchestrator)

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert cancelling_runner.calls == 1
    assert result.precedence is not None
    assert result.precedence.outcome is RunOutcome.PROVIDER_ERROR  # retryable in itself
    assert result.retry_decision is not None
    assert result.retry_decision.should_retry is False  # ... suppressed by cancellation


# --- Provider retry loop: guard matrix, recovery, exhaustion (M12-04) -------


def test_run_provider_attempts_retries_a_trusted_error_then_recovers() -> None:
    diagnostic = _provider_diagnostic(retryable=True)
    first_attempt = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        outcome=RunOutcome.PROVIDER_ERROR,
        provider_diagnostic=diagnostic,
    )
    second_attempt = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=2,
        terminal_response=_success_response(AgentRole.CODER),
    )
    sleeper = RecordingSleeper()
    run_store = RecordingRunStorePort([], sink=RecordingAttemptLogSink())
    git_safety = RecordingGitSafetyPort(
        [], [*_safe_pair_unchanged(), *_safe_pair_unchanged()]
    )
    agent_runner = QueuedAgentRunner([first_attempt, second_attempt])
    opencode_preflight = RecordingOpenCodePreflightPort([])
    orchestrator = IssueOrchestrator(
        initial_record=_initial_record(run_id="run-001", target=_target()),
        agent_runner=agent_runner,
        run_store=run_store,
        git_safety=git_safety,
        opencode_preflight=opencode_preflight,
        control_plane_digest=CONTROL_PLANE_DIGEST,
        clock=SteppingClock(start=NOW),
        sleeper=sleeper,
        provider_retry=_provider_retry_config(),
    )

    results = orchestrator.run_provider_attempts(
        role=AgentRole.CODER,
        review_cycle=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert [result.provider_attempt for result in results] == [1, 2]
    assert len(agent_runner.calls) == 2
    assert sleeper.calls == [1.0]  # compute_backoff_delay_seconds(1, config)
    assert results[0].retry_decision is not None
    assert results[0].retry_decision.should_retry is True
    assert results[1].retry_decision is not None
    assert results[1].retry_decision.should_retry is False
    assert len(run_store.persist_calls) == 2

    # The control-plane digest is rechecked once per provider attempt --
    # including the retry -- always against the same, never-recomputed
    # digest `verify` originally returned (System Design SS18.2; ADR-005).
    # `verify()` itself is never called here: this fake raises if it ever
    # is (see `RecordingOpenCodePreflightPort.verify`), so the retry loop
    # completing at all already proves it stayed uncalled across both
    # attempts -- `verify()` is `bootstrap_run`'s (M13-01) sole concern.
    assert opencode_preflight.recheck_calls == [
        CONTROL_PLANE_DIGEST,
        CONTROL_PLANE_DIGEST,
    ]

    # source/signature stay intact on the persisted attempt that carried them.
    first_persisted_attempt = run_store.persist_calls[0].attempts[0]
    assert first_persisted_attempt.agent_result.provider_diagnostic is not None
    assert first_persisted_attempt.agent_result.provider_diagnostic.source == (
        diagnostic.source
    )
    assert first_persisted_attempt.agent_result.provider_diagnostic.signature == (
        diagnostic.signature
    )


def test_run_provider_attempts_does_not_retry_an_untrusted_lookalike_diagnostic() -> (
    None
):
    """AC: "Lookalike non trusted non ritenta" -- a diagnostic is present
    (the attempt still classifies as `PROVIDER_ERROR`) but is not itself
    marked retryable, so no retry is authorized."""

    sleeper = RecordingSleeper()
    orchestrator, _run_store, agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                outcome=RunOutcome.PROVIDER_ERROR,
                provider_diagnostic=_provider_diagnostic(retryable=False),
            ),
            git_results=_safe_pair_unchanged(),
            clock=SteppingClock(start=NOW),
            sleeper=sleeper,
        )
    )

    results = orchestrator.run_provider_attempts(
        role=AgentRole.CODER,
        review_cycle=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert len(results) == 1
    assert len(agent_runner.calls) == 1
    assert sleeper.calls == []
    assert results[0].precedence is not None
    assert results[0].precedence.outcome is RunOutcome.PROVIDER_ERROR
    assert results[0].retry_decision is not None
    assert results[0].retry_decision.should_retry is False


def test_run_provider_attempts_stops_after_unconfirmed_termination() -> None:
    """`termination_confirmed` is read straight from the process result,
    independent of the classified outcome: a `PROVIDER_ERROR` precedence
    (the diagnostic's own presence outranks a concurrent `PROCESS_ERROR`
    signal, System Design SS13.2) still respects an unconfirmed process
    join as its own, separate retry guard."""

    sleeper = RecordingSleeper()
    orchestrator, _run_store, agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                outcome=RunOutcome.PROVIDER_ERROR,
                provider_diagnostic=_provider_diagnostic(retryable=True),
                process=_agent_process_result(
                    outcome=RunOutcome.PROCESS_ERROR,
                    return_code=1,
                    termination_confirmed=False,
                ),
            ),
            git_results=_safe_pair_unchanged(),
            clock=SteppingClock(start=NOW),
            sleeper=sleeper,
        )
    )

    results = orchestrator.run_provider_attempts(
        role=AgentRole.CODER,
        review_cycle=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert len(results) == 1
    assert len(agent_runner.calls) == 1
    assert sleeper.calls == []
    assert results[0].retry_decision is not None
    assert results[0].retry_decision.should_retry is False


def test_run_provider_attempts_stops_after_an_unsafe_git_after() -> None:
    sleeper = RecordingSleeper()
    unsafe_after_pair = [
        _git_check(
            sequence=0, purpose="before", state=_git_state(fingerprint="fp-unchanged")
        ),
        _git_check(
            sequence=1,
            purpose="after",
            safety_status=GitSafetyStatus.UNSAFE,
            state=_git_state(fingerprint="fp-unchanged"),
        ),
    ]
    orchestrator, _run_store, agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                outcome=RunOutcome.PROVIDER_ERROR,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
            git_results=unsafe_after_pair,
            clock=SteppingClock(start=NOW),
            sleeper=sleeper,
        )
    )

    results = orchestrator.run_provider_attempts(
        role=AgentRole.CODER,
        review_cycle=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert len(results) == 1
    assert len(agent_runner.calls) == 1
    assert sleeper.calls == []
    assert results[0].retry_decision is not None
    assert results[0].retry_decision.should_retry is False


def test_run_provider_attempts_stops_and_preserves_output_on_a_coder_mutation() -> None:
    """AC: "Coder mutation sopprime retry e preserva modifiche" -- a changed
    target fingerprint on a coder attempt suppresses the retry outright
    (`retry_suppressed_due_to_target_change`), rather than letting a fresh
    attempt overwrite the coder's own partial work."""

    sleeper = RecordingSleeper()
    orchestrator, _run_store, agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                outcome=RunOutcome.PROVIDER_ERROR,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
            git_results=_safe_pair(),  # before/after fingerprints differ
            clock=SteppingClock(start=NOW),
            sleeper=sleeper,
        )
    )

    results = orchestrator.run_provider_attempts(
        role=AgentRole.CODER,
        review_cycle=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert len(results) == 1
    assert len(agent_runner.calls) == 1
    assert sleeper.calls == []
    assert results[0].retry_decision is not None
    assert results[0].retry_decision.should_retry is False
    assert results[0].retry_decision.retry_suppressed_due_to_target_change is True


def test_run_provider_attempts_exhausts_the_budget_without_touching_review_cycle() -> (
    None
):
    config = ProviderRetryConfig(
        max_attempts=3,
        initial_delay_seconds=1.0,
        multiplier=2.0,
        max_delay_seconds=30.0,
    )
    diagnostic = _provider_diagnostic(retryable=True)
    agent_results = [
        _agent_result(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=attempt,
            outcome=RunOutcome.PROVIDER_ERROR,
            provider_diagnostic=diagnostic,
        )
        for attempt in (1, 2, 3)
    ]
    sleeper = RecordingSleeper()
    run_store = RecordingRunStorePort([], sink=RecordingAttemptLogSink())
    git_safety = RecordingGitSafetyPort(
        [],
        [*_safe_pair_unchanged(), *_safe_pair_unchanged(), *_safe_pair_unchanged()],
    )
    agent_runner = QueuedAgentRunner(agent_results)
    opencode_preflight = RecordingOpenCodePreflightPort([])
    orchestrator = IssueOrchestrator(
        initial_record=_initial_record(run_id="run-001", target=_target()),
        agent_runner=agent_runner,
        run_store=run_store,
        git_safety=git_safety,
        opencode_preflight=opencode_preflight,
        control_plane_digest=CONTROL_PLANE_DIGEST,
        clock=SteppingClock(start=NOW),
        sleeper=sleeper,
        provider_retry=config,
    )

    results = orchestrator.run_provider_attempts(
        role=AgentRole.CODER,
        review_cycle=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert [result.provider_attempt for result in results] == [1, 2, 3]
    assert len(agent_runner.calls) == 3  # exactly the configured budget, no more
    assert sleeper.calls == [1.0, 2.0]  # two sleeps between three attempts
    assert [result.state.review_cycle for result in results] == [1, 1, 1]
    assert results[-1].retry_decision is not None
    assert results[-1].retry_decision.should_retry is False  # budget exhausted
    assert results[-1].precedence is not None
    assert results[-1].precedence.outcome is RunOutcome.PROVIDER_ERROR
    assert len(run_store.persist_calls) == 3

    # Rechecked once per attempt -- three attempts, three rechecks, every
    # one against the exact same digest (never recomputed mid-run).
    assert opencode_preflight.recheck_calls == [CONTROL_PLANE_DIGEST] * 3


def test_run_provider_attempts_uses_the_exact_capped_backoff_delay() -> None:
    config = ProviderRetryConfig(
        max_attempts=4,
        initial_delay_seconds=1.0,
        multiplier=3.0,
        max_delay_seconds=5.0,
    )
    diagnostic = _provider_diagnostic(retryable=True)
    agent_results = [
        _agent_result(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=attempt,
            outcome=RunOutcome.PROVIDER_ERROR,
            provider_diagnostic=diagnostic,
        )
        for attempt in (1, 2, 3, 4)
    ]
    sleeper = RecordingSleeper()
    run_store = RecordingRunStorePort([], sink=RecordingAttemptLogSink())
    git_safety = RecordingGitSafetyPort([], [*_safe_pair_unchanged()] * 4)
    agent_runner = QueuedAgentRunner(agent_results)
    orchestrator = IssueOrchestrator(
        initial_record=_initial_record(run_id="run-001", target=_target()),
        agent_runner=agent_runner,
        run_store=run_store,
        git_safety=git_safety,
        opencode_preflight=RecordingOpenCodePreflightPort([]),
        control_plane_digest=CONTROL_PLANE_DIGEST,
        clock=SteppingClock(start=NOW),
        sleeper=sleeper,
        provider_retry=config,
    )

    orchestrator.run_provider_attempts(
        role=AgentRole.CODER,
        review_cycle=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    # delay(1)=1.0, delay(2)=3.0, delay(3)=9.0 capped to max_delay_seconds=5.0
    assert sleeper.calls == [1.0, 3.0, 5.0]


def test_run_provider_attempts_raises_and_never_sleeps_on_a_persist_failure() -> None:
    sleeper = RecordingSleeper()
    orchestrator, run_store, agent_runner, _git_safety, _opencode_preflight = (
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                outcome=RunOutcome.PROVIDER_ERROR,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
            git_results=_safe_pair_unchanged(),
            clock=SteppingClock(start=NOW),
            sleeper=sleeper,
            persist_results=[PersistenceStatus.FAILED],
        )
    )

    with pytest.raises(LoggingError):
        orchestrator.run_provider_attempts(
            role=AgentRole.CODER,
            review_cycle=1,
            prompt="Implement the fix.",
            workspace=_workspace(),
        )

    assert len(agent_runner.calls) == 1
    assert sleeper.calls == []
    assert len(run_store.persist_calls) == 1


def test_run_provider_attempts_stops_after_cancellation_observed_mid_attempt() -> None:
    sleeper = RecordingSleeper()
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        outcome=RunOutcome.PROVIDER_ERROR,
        provider_diagnostic=_provider_diagnostic(retryable=True),
    )
    call_log: list[str] = []
    run_store = RecordingRunStorePort(call_log, sink=RecordingAttemptLogSink())
    git_safety = RecordingGitSafetyPort(call_log, _safe_pair_unchanged())
    orchestrator_box: list[IssueOrchestrator] = []
    cancelling_runner = CancellingAgentRunner(
        result=agent_result,
        on_run=lambda: orchestrator_box[0].request_cancellation(),
    )
    orchestrator = IssueOrchestrator(
        initial_record=_initial_record(run_id="run-001", target=_target()),
        agent_runner=cancelling_runner,
        run_store=run_store,
        git_safety=git_safety,
        opencode_preflight=RecordingOpenCodePreflightPort(call_log),
        control_plane_digest=CONTROL_PLANE_DIGEST,
        clock=SteppingClock(start=NOW),
        sleeper=sleeper,
        provider_retry=_provider_retry_config(),
    )
    orchestrator_box.append(orchestrator)

    results = orchestrator.run_provider_attempts(
        role=AgentRole.CODER,
        review_cycle=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert len(results) == 1
    assert cancelling_runner.calls == 1
    assert sleeper.calls == []
    assert results[0].retry_decision is not None
    assert results[0].retry_decision.should_retry is False


# --- Typed result construction with orthogonal fields -----------------------


def test_logical_invocation_result_requires_matching_phase_for_role() -> None:
    mismatched_state = _pipeline_state(role=AgentRole.CODER, review_cycle=1)
    with pytest.raises(ValueError, match="state.phase must match role"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:ARCHITECT:0",
            role=AgentRole.ARCHITECT,
            state=mismatched_state,
            provider_attempt=1,
            git_before=_git_check(sequence=0, purpose="before"),
            control_plane_error=None,
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
            ),
            git_after=_git_check(sequence=1, purpose="after"),
            precedence=AttemptPrecedence(
                outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
            ),
            retry_decision=_no_retry_decision(),
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_a_blocked_result_to_have_no_agent_data() -> (
    None
):
    unsafe_before = _git_check(
        sequence=0, purpose="before", safety_status=GitSafetyStatus.UNSAFE
    )
    with pytest.raises(ValueError, match="agent_result must be None"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=unsafe_before,
            control_plane_error=None,
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_after=None,
            precedence=None,
            retry_decision=None,
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_a_blocked_result_to_have_no_retry_decision() -> (
    None
):
    unsafe_before = _git_check(
        sequence=0, purpose="before", safety_status=GitSafetyStatus.UNSAFE
    )
    with pytest.raises(ValueError, match="retry_decision must be None"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=unsafe_before,
            control_plane_error=None,
            agent_result=None,
            git_after=None,
            precedence=None,
            retry_decision=_no_retry_decision(),
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_an_unblocked_result_to_have_agent_data() -> (
    None
):
    safe_before = _git_check(sequence=0, purpose="before")
    with pytest.raises(TypeError, match="agent_result must be AgentResult"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=safe_before,
            control_plane_error=None,
            agent_result=None,
            git_after=None,
            precedence=None,
            retry_decision=None,
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_an_unblocked_result_to_have_a_retry_decision() -> (
    None
):
    safe_before = _git_check(sequence=0, purpose="before")
    with pytest.raises(TypeError, match="retry_decision must be RetryDecision"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=safe_before,
            control_plane_error=None,
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_after=_git_check(sequence=1, purpose="after"),
            precedence=AttemptPrecedence(
                outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
            ),
            retry_decision=None,
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_agent_result_role_to_match() -> None:
    with pytest.raises(ValueError, match="agent_result role must match role"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=_git_check(sequence=0, purpose="before"),
            control_plane_error=None,
            agent_result=_agent_result(
                role=AgentRole.REVIEWER, review_cycle=1, provider_attempt=1
            ),
            git_after=_git_check(sequence=1, purpose="after"),
            precedence=AttemptPrecedence(
                outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
            ),
            retry_decision=_no_retry_decision(),
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_rejects_empty_events() -> None:
    with pytest.raises(ValueError, match="events must not be empty"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=_git_check(sequence=0, purpose="before"),
            control_plane_error=None,
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_after=_git_check(sequence=1, purpose="after"),
            precedence=AttemptPrecedence(
                outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
            ),
            retry_decision=_no_retry_decision(),
            events=(),
        )


def test_logical_invocation_result_rejects_non_sequential_events() -> None:
    with pytest.raises(ValueError, match="sequential"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=_git_check(sequence=0, purpose="before"),
            control_plane_error=None,
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_after=_git_check(sequence=1, purpose="after"),
            precedence=AttemptPrecedence(
                outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
            ),
            retry_decision=_no_retry_decision(),
            events=(
                InvocationEvent(
                    sequence=1,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_keeps_git_and_technical_dimensions_orthogonal() -> (
    None
):
    """A technically successful attempt can still carry an UNSAFE `git_after`
    -- the two dimensions are reported side by side, never merged."""

    unsafe_after = _git_check(
        sequence=1, purpose="after", safety_status=GitSafetyStatus.UNSAFE
    )
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        terminal_response=_success_response(AgentRole.CODER),
    )

    result = LogicalInvocationResult(
        logical_invocation_id="run-001:CODER:1",
        role=AgentRole.CODER,
        state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
        provider_attempt=1,
        git_before=_git_check(sequence=0, purpose="before"),
        control_plane_error=None,
        agent_result=agent_result,
        git_after=unsafe_after,
        precedence=AttemptPrecedence(
            outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
        ),
        retry_decision=_no_retry_decision(),
        events=(
            InvocationEvent(
                sequence=0, kind=InvocationEventKind.INVOCATION_STARTED, timestamp=NOW
            ),
        ),
    )

    assert result.precedence is not None
    assert result.precedence.outcome is RunOutcome.SUCCEEDED
    assert result.git_after is not None
    assert result.git_after.safety_status is GitSafetyStatus.UNSAFE

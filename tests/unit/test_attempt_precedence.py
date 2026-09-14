"""Unit tests for the port-only logical invocation skeleton (M12-01).

Exercises `orchestrator.IssueOrchestrator` purely against `Fake`/`Recording`
doubles written directly against `ports.py` `Protocol`s -- no subprocess, no
real clock, no real Git or persistence. Covers exactly M12-01's acceptance
criteria: the module's own import boundary, dependency injection, unambiguous
logical-invocation identity and counters, fakes observing call order and
input, and construction of a typed result with orthogonal fields. The
attempt-outcome precedence classifier itself (`TIMEOUT > PROVIDER_ERROR >
...`) is System Design SS13.2 / M12-02 and is not yet exercised here; this
file's name anticipates that later coverage.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    RunOutcome,
    RunRecord,
    Workspace,
)
from opencode_tools.orchestrator import (
    InvocationEvent,
    InvocationEventKind,
    IssueOrchestrator,
    LogicalInvocationResult,
)
from opencode_tools.ports import AttemptLogSink, LogChannel
from opencode_tools.state_machine import PipelineState

_ALLOWED_INTERNAL_MODULES = frozenset(
    {
        "opencode_tools.domain",
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


def _process_result() -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/opencode", "run"),
        cwd=WORKSPACE_ROOT,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=5),
        duration_ns=5_000_000_000,
        return_code=0,
        timed_out=False,
        termination_confirmed=True,
        log_path=Path("attempt.log"),
        stdout_byte_count=0,
        stdout_sha256="stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="stderr-digest",
        outcome=RunOutcome.SUCCEEDED,
    )


def _agent_result(
    *, role: AgentRole, review_cycle: int | None, provider_attempt: int
) -> AgentResult:
    return AgentResult(
        role=role,
        phase=_PHASE_BY_ROLE[role],
        review_cycle=review_cycle,
        provider_attempt=provider_attempt,
        process=_process_result(),
        terminal_response=ParsedAgentResponse(role=role, body="ready"),
        session_id="session-001",
        verified_agent=role.value.lower(),
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )


def _pipeline_state(*, role: AgentRole, review_cycle: int | None) -> PipelineState:
    return PipelineState(phase=_PHASE_BY_ROLE[role], review_cycle=review_cycle)


def _invocation_result(
    *,
    role: AgentRole = AgentRole.CODER,
    review_cycle: int | None = 1,
    provider_attempt: int = 1,
    agent_result: AgentResult | None = None,
    events: tuple[InvocationEvent, ...] | None = None,
) -> LogicalInvocationResult:
    resolved_agent_result = (
        agent_result
        if agent_result is not None
        else _agent_result(
            role=role, review_cycle=review_cycle, provider_attempt=provider_attempt
        )
    )
    resolved_events = (
        events
        if events is not None
        else (
            InvocationEvent(
                sequence=0,
                kind=InvocationEventKind.INVOCATION_STARTED,
                timestamp=NOW,
            ),
        )
    )
    return LogicalInvocationResult(
        logical_invocation_id="run-001:CODER:1",
        role=role,
        state=_pipeline_state(role=role, review_cycle=review_cycle),
        provider_attempt=provider_attempt,
        agent_result=resolved_agent_result,
        events=resolved_events,
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

    def __init__(self, call_log: list[str], *, sink: RecordingAttemptLogSink) -> None:
        self._call_log = call_log
        self._sink = sink
        self.sink_calls: list[tuple[AgentRole, int | None, int]] = []

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        raise AssertionError("not exercised by the M12-01 skeleton")

    def open_attempt_sink(
        self,
        role: AgentRole,
        review_cycle: int | None,
        provider_attempt: int,
    ) -> AttemptLogSink:
        self._call_log.append("run_store.open_attempt_sink")
        self.sink_calls.append((role, review_cycle, provider_attempt))
        return self._sink

    def persist(self, record: RunRecord) -> PersistenceStatus:
        raise AssertionError("not exercised by the M12-01 skeleton")


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
        raise AssertionError("not exercised by the M12-01 skeleton")


def _orchestrator(
    *,
    call_log: list[str],
    sink: RecordingAttemptLogSink,
    agent_result: AgentResult,
    clock: SteppingClock,
    run_id: str = "run-001",
) -> tuple[IssueOrchestrator, RecordingRunStorePort, ScriptedAgentRunner]:
    run_store = RecordingRunStorePort(call_log, sink=sink)
    agent_runner = ScriptedAgentRunner(call_log, result=agent_result)
    orchestrator = IssueOrchestrator(
        run_id=run_id,
        agent_runner=agent_runner,
        run_store=run_store,
        clock=clock,
    )
    return orchestrator, run_store, agent_runner


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

    orchestrator, run_store, agent_runner = _orchestrator(
        call_log=call_log, sink=sink, agent_result=result, clock=clock
    )

    assert isinstance(orchestrator, IssueOrchestrator)
    assert run_store.sink_calls == []
    assert agent_runner.calls == []


def test_orchestrator_rejects_empty_run_id() -> None:
    sink = RecordingAttemptLogSink()
    result = _agent_result(
        role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
    )
    with pytest.raises(ValueError, match="run_id"):
        _orchestrator(
            call_log=[],
            sink=sink,
            agent_result=result,
            clock=SteppingClock(start=NOW),
            run_id="",
        )


# --- ID / counter invariants and call recording -----------------------------


def test_run_logical_invocation_opens_sink_before_invoking_agent_in_order() -> None:
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    agent_result = _agent_result(
        role=AgentRole.CODER, review_cycle=2, provider_attempt=1
    )
    clock = SteppingClock(start=NOW)
    orchestrator, run_store, agent_runner = _orchestrator(
        call_log=call_log, sink=sink, agent_result=agent_result, clock=clock
    )
    workspace = _workspace()

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=2,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    assert call_log == ["run_store.open_attempt_sink", "agent_runner.run"]
    assert run_store.sink_calls == [(AgentRole.CODER, 2, 1)]
    assert agent_runner.calls == [
        (AgentRole.CODER, "Implement the fix.", workspace, 2, 1, sink)
    ]
    assert sink.closed is True
    assert result.agent_result is agent_result
    assert [event.kind for event in result.events] == [
        InvocationEventKind.INVOCATION_STARTED,
        InvocationEventKind.ATTEMPT_SINK_OPENED,
        InvocationEventKind.AGENT_RESULT_RECEIVED,
        InvocationEventKind.ATTEMPT_SINK_CLOSED,
    ]
    assert [event.sequence for event in result.events] == [0, 1, 2, 3]
    timestamps = [event.timestamp for event in result.events]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == 4
    assert clock.calls == 4


def test_run_logical_invocation_assigns_the_same_identity_across_provider_attempts() -> (
    None
):
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    clock = SteppingClock(start=NOW)
    agent_result_attempt_1 = _agent_result(
        role=AgentRole.CODER, review_cycle=1, provider_attempt=1
    )
    orchestrator, _run_store, agent_runner = _orchestrator(
        call_log=call_log, sink=sink, agent_result=agent_result_attempt_1, clock=clock
    )
    workspace = _workspace()

    first = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    agent_result_attempt_2 = _agent_result(
        role=AgentRole.CODER, review_cycle=1, provider_attempt=2
    )
    agent_runner.queue_result(agent_result_attempt_2)  # simulate a retried attempt

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


def test_run_logical_invocation_assigns_distinct_identity_per_role_and_cycle() -> None:
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    clock = SteppingClock(start=NOW)
    workspace = _workspace()

    def _run(role: AgentRole, review_cycle: int | None) -> LogicalInvocationResult:
        agent_result = _agent_result(
            role=role, review_cycle=review_cycle, provider_attempt=1
        )
        orchestrator, _run_store, _agent_runner = _orchestrator(
            call_log=call_log, sink=sink, agent_result=agent_result, clock=clock
        )
        return orchestrator.run_logical_invocation(
            role=role,
            review_cycle=review_cycle,
            provider_attempt=1,
            prompt="prompt",
            workspace=workspace,
        )

    architect = _run(AgentRole.ARCHITECT, None)
    coder_cycle_1 = _run(AgentRole.CODER, 1)
    coder_cycle_2 = _run(AgentRole.CODER, 2)
    reviewer_cycle_1 = _run(AgentRole.REVIEWER, 1)

    ids = {
        architect.logical_invocation_id,
        coder_cycle_1.logical_invocation_id,
        coder_cycle_2.logical_invocation_id,
        reviewer_cycle_1.logical_invocation_id,
    }
    assert len(ids) == 4
    assert architect.state.review_cycle is None
    assert coder_cycle_1.state.review_cycle == 1
    assert coder_cycle_2.state.review_cycle == 2
    assert reviewer_cycle_1.state.review_cycle == 1


def test_run_logical_invocation_rejects_architect_with_a_review_cycle() -> None:
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    agent_result = _agent_result(
        role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
    )
    orchestrator, _run_store, _agent_runner = _orchestrator(
        call_log=call_log,
        sink=sink,
        agent_result=agent_result,
        clock=SteppingClock(start=NOW),
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
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    agent_result = _agent_result(
        role=AgentRole.CODER, review_cycle=1, provider_attempt=1
    )
    orchestrator, _run_store, _agent_runner = _orchestrator(
        call_log=call_log,
        sink=sink,
        agent_result=agent_result,
        clock=SteppingClock(start=NOW),
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
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    agent_result = _agent_result(
        role=AgentRole.CODER, review_cycle=1, provider_attempt=1
    )
    orchestrator, _run_store, _agent_runner = _orchestrator(
        call_log=call_log,
        sink=sink,
        agent_result=agent_result,
        clock=SteppingClock(start=NOW),
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
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    agent_result = _agent_result(
        role=AgentRole.CODER, review_cycle=1, provider_attempt=1
    )
    orchestrator, _run_store, _agent_runner = _orchestrator(
        call_log=call_log,
        sink=sink,
        agent_result=agent_result,
        clock=SteppingClock(start=NOW),
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


# --- Typed result construction with orthogonal fields -----------------------


def test_logical_invocation_result_requires_matching_phase_for_role() -> None:
    mismatched_state = _pipeline_state(role=AgentRole.CODER, review_cycle=1)
    with pytest.raises(ValueError, match="state.phase must match role"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:ARCHITECT:0",
            role=AgentRole.ARCHITECT,
            state=mismatched_state,
            provider_attempt=1,
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
            ),
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
            agent_result=_agent_result(
                role=AgentRole.REVIEWER, review_cycle=1, provider_attempt=1
            ),
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_agent_result_review_cycle_to_match() -> (
    None
):
    with pytest.raises(ValueError, match="review_cycle"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=2, provider_attempt=1
            ),
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_agent_result_provider_attempt_to_match() -> (
    None
):
    with pytest.raises(ValueError, match="provider_attempt"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=2
            ),
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
        _invocation_result(events=())


def test_logical_invocation_result_rejects_non_sequential_events() -> None:
    with pytest.raises(ValueError, match="sequential"):
        _invocation_result(
            events=(
                InvocationEvent(
                    sequence=1,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            )
        )


def test_logical_invocation_result_rejects_wrong_event_type() -> None:
    with pytest.raises(TypeError, match="InvocationEvent"):
        _invocation_result(events=cast(tuple[InvocationEvent, ...], ("not-an-event",)))


def test_logical_invocation_result_rejects_wrong_agent_result_type() -> None:
    with pytest.raises(TypeError, match="AgentResult"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            agent_result=cast(AgentResult, "not-a-result"),
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_keeps_role_state_and_attempt_orthogonal() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER, review_cycle=3, provider_attempt=2
    )
    events = (
        InvocationEvent(
            sequence=0, kind=InvocationEventKind.INVOCATION_STARTED, timestamp=NOW
        ),
        InvocationEvent(
            sequence=1,
            kind=InvocationEventKind.ATTEMPT_SINK_OPENED,
            timestamp=NOW + timedelta(seconds=1),
        ),
    )

    result = LogicalInvocationResult(
        logical_invocation_id="run-001:CODER:3",
        role=AgentRole.CODER,
        state=_pipeline_state(role=AgentRole.CODER, review_cycle=3),
        provider_attempt=2,
        agent_result=agent_result,
        events=events,
    )

    assert result.role is AgentRole.CODER
    assert result.state.review_cycle == 3
    assert result.provider_attempt == 2
    assert result.agent_result is agent_result
    assert result.events == events

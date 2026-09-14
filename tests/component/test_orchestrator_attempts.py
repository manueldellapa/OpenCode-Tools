"""Component tests for the port-only logical invocation skeleton (M12-01).

Drives `orchestrator.IssueOrchestrator` across a short, realistic sequence of
logical invocations (architect, a coder/reviewer cycle, a rework cycle, and a
retried provider attempt) using real `tmp_path`-derived absolute paths for
`Workspace`, exactly as a composed run would see them, while every port stays
a fake: M12-01 explicitly excludes concrete adapters, subprocesses, Git
checkpoints, and real persistence. Those integration behaviors (control-plane
precedence, retry guards, persistence fault handling) belong to M12-02
through M12-04; this file only proves the skeleton's identity/counter and
call-order guarantees hold across more than one invocation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    InvocationEventKind,
    IssueOrchestrator,
    LogicalInvocationResult,
)
from opencode_tools.ports import AttemptLogSink, LogChannel

_PHASE_BY_ROLE: dict[AgentRole, PipelinePhase] = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}

NOW = datetime(2026, 9, 14, 9, 0, 0, tzinfo=UTC)


def _process_result(*, workspace_root: Path) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/opencode", "run"),
        cwd=workspace_root,
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
    *,
    role: AgentRole,
    review_cycle: int | None,
    provider_attempt: int,
    workspace_root: Path,
) -> AgentResult:
    return AgentResult(
        role=role,
        phase=_PHASE_BY_ROLE[role],
        review_cycle=review_cycle,
        provider_attempt=provider_attempt,
        process=_process_result(workspace_root=workspace_root),
        terminal_response=ParsedAgentResponse(role=role, body="ready"),
        session_id=f"session-{role.value.lower()}-{review_cycle or 0}-{provider_attempt}",
        verified_agent=role.value.lower(),
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )


class RecordingAttemptLogSink:
    """A minimal `AttemptLogSink` fake that records writes and closing."""

    def __init__(self, path: Path) -> None:
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


class SequencedRunStorePort:
    """A `RunStorePort` fake that opens one fresh sink per attempt."""

    def __init__(self) -> None:
        self.sink_calls: list[tuple[AgentRole, int | None, int]] = []
        self.opened_sinks: list[RecordingAttemptLogSink] = []

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        raise AssertionError("not exercised by the M12-01 skeleton")

    def open_attempt_sink(
        self,
        role: AgentRole,
        review_cycle: int | None,
        provider_attempt: int,
    ) -> AttemptLogSink:
        self.sink_calls.append((role, review_cycle, provider_attempt))
        cycle_component = review_cycle if review_cycle is not None else 0
        sink = RecordingAttemptLogSink(
            path=Path(f"{role.value.lower()}-{cycle_component}-{provider_attempt}.log")
        )
        self.opened_sinks.append(sink)
        return sink

    def persist(self, record: RunRecord) -> PersistenceStatus:
        raise AssertionError("not exercised by the M12-01 skeleton")


class SequencedAgentRunner:
    """An `AgentRunner` fake returning one scripted result per call, in order."""

    def __init__(self, results: list[AgentResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[AgentRole, str, Workspace, int | None, int]] = []

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
        self.calls.append((role, prompt, workspace, review_cycle, provider_attempt))
        return self._results.pop(0)


class SteppingClock:
    """A `Clock` fake whose `now()` advances by one second on every call."""

    def __init__(self, *, start: datetime) -> None:
        self._next = start

    def now(self) -> datetime:
        current = self._next
        self._next = current + timedelta(seconds=1)
        return current

    def monotonic_ns(self) -> int:
        raise AssertionError("not exercised by the M12-01 skeleton")


def test_logical_invocations_across_a_rework_cycle_keep_unambiguous_identity(
    tmp_path: Path,
) -> None:
    """A skeleton architect -> coder -> reviewer -> coder rework sequence.

    No adapter or subprocess is ever constructed; every side effect goes
    through the injected `AgentRunner`/`RunStorePort`/`Clock` fakes, and the
    scenario proves each logical invocation keeps one stable identity across
    a provider retry while remaining distinct from every other role/cycle.
    """

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace = Workspace(root=workspace_root)

    plan = [
        (AgentRole.ARCHITECT, None, 1),
        (AgentRole.CODER, 1, 1),
        (AgentRole.REVIEWER, 1, 1),
        (AgentRole.CODER, 2, 1),
        (AgentRole.CODER, 2, 2),  # a provider retry of the same logical invocation
    ]
    results = [
        _agent_result(
            role=role,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            workspace_root=workspace_root,
        )
        for role, review_cycle, provider_attempt in plan
    ]

    run_store = SequencedRunStorePort()
    agent_runner = SequencedAgentRunner(results)
    orchestrator = IssueOrchestrator(
        run_id="run-2026-09-14-abcd",
        agent_runner=agent_runner,
        run_store=run_store,
        clock=SteppingClock(start=NOW),
    )

    outcomes: list[LogicalInvocationResult] = [
        orchestrator.run_logical_invocation(
            role=role,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            prompt=f"Attempt {provider_attempt} of {role.value} cycle {review_cycle}.",
            workspace=workspace,
        )
        for role, review_cycle, provider_attempt in plan
    ]

    architect, coder_1, reviewer_1, coder_2_attempt_1, coder_2_attempt_2 = outcomes

    # Every provider attempt actually reached the agent runner, in the exact
    # planned order, carrying the real workspace and its own attempt sink.
    assert [
        (role, review_cycle, provider_attempt)
        for role, _prompt, _workspace, review_cycle, provider_attempt in (
            agent_runner.calls
        )
    ] == [(role, cycle, attempt) for role, cycle, attempt in plan]
    assert all(call[2] == workspace for call in agent_runner.calls)
    assert run_store.sink_calls == [
        (role, cycle, attempt) for role, cycle, attempt in plan
    ]
    assert len(run_store.opened_sinks) == 5
    assert all(sink.closed for sink in run_store.opened_sinks)
    assert len({id(sink) for sink in run_store.opened_sinks}) == 5

    # Every logical invocation is unambiguous: role/cycle pairs that differ
    # get distinct identities, and identity survives a provider retry.
    ids = [outcome.logical_invocation_id for outcome in outcomes]
    assert len(set(ids[:4])) == 4
    assert (
        coder_2_attempt_1.logical_invocation_id
        == coder_2_attempt_2.logical_invocation_id
    )
    assert coder_2_attempt_1.provider_attempt == 1
    assert coder_2_attempt_2.provider_attempt == 2

    # Role/phase/cycle/provider-attempt remain distinct, orthogonal fields.
    assert architect.state.phase is PipelinePhase.ARCHITECT
    assert architect.state.review_cycle is None
    assert coder_1.state.phase is PipelinePhase.CODER
    assert coder_1.state.review_cycle == 1
    assert reviewer_1.state.phase is PipelinePhase.REVIEWER
    assert reviewer_1.state.review_cycle == 1
    assert coder_2_attempt_1.state.review_cycle == 2
    assert coder_2_attempt_2.state.review_cycle == 2

    # Each invocation's own event trail is complete and strictly ordered.
    for outcome in outcomes:
        assert [event.kind for event in outcome.events] == [
            InvocationEventKind.INVOCATION_STARTED,
            InvocationEventKind.ATTEMPT_SINK_OPENED,
            InvocationEventKind.AGENT_RESULT_RECEIVED,
            InvocationEventKind.ATTEMPT_SINK_CLOSED,
        ]
        assert [event.sequence for event in outcome.events] == [0, 1, 2, 3]
        timestamps = [event.timestamp for event in outcome.events]
        assert timestamps == sorted(timestamps)

"""Coordinates exactly one logical invocation of a single agent role.

`IssueOrchestrator` is the first, skeleton shape of the applicative
coordinator described by System Design SS5.2/SS8.3: it depends only on
`domain`, `ports`, and the pure `state_machine` policy, is constructed with
already-built ports, and never imports a concrete adapter, spawns a
subprocess, parses CLI arguments or raw protocol text, or performs any
reasoning about the issue or the code (System Design SS6; ADR-001).

This module intentionally implements only System Design SS8.3's shape, not
yet its full mandatory sequence: `run_logical_invocation` assigns an
unambiguous logical invocation identity, opens the attempt's exclusive sink,
invokes the agent once, and closes the sink, producing a typed
`LogicalInvocationResult` with an ordered event trail and orthogonal
role/phase/cycle/provider-attempt fields. Git checkpoints and control-plane
rechecks (M12-02), attempt persistence and cancellation handling (M12-03),
and the provider retry guard (M12-04) are added by later issues; full
pipeline composition, the review rework loop, and the `cli.py` composition
root (System Design SS5.3) remain out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from opencode_tools.domain import AgentResult, AgentRole, PipelinePhase, Workspace
from opencode_tools.ports import AgentRunner, Clock, RunStorePort
from opencode_tools.state_machine import PipelineState

_PHASE_BY_ROLE: dict[AgentRole, PipelinePhase] = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}


def _require_exact_enum(
    value: object, enum_type: type[StrEnum], field_name: str
) -> None:
    if type(value) is not enum_type:
        raise TypeError(f"{field_name} must be {enum_type.__name__}")


def _require_int(value: object, field_name: str, *, minimum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")


def _require_non_empty(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value:
        raise ValueError(f"{field_name} must not be empty")


def _require_utc(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


class InvocationEventKind(StrEnum):
    """One step of this skeleton's slice of the mandatory sequence (SS8.3)."""

    INVOCATION_STARTED = "INVOCATION_STARTED"
    ATTEMPT_SINK_OPENED = "ATTEMPT_SINK_OPENED"
    AGENT_RESULT_RECEIVED = "AGENT_RESULT_RECEIVED"
    ATTEMPT_SINK_CLOSED = "ATTEMPT_SINK_CLOSED"


@dataclass(frozen=True, slots=True)
class InvocationEvent:
    """One ordered, timestamped step of a logical invocation's own timeline."""

    sequence: int
    kind: InvocationEventKind
    timestamp: datetime

    def __post_init__(self) -> None:
        _require_int(self.sequence, "sequence", minimum=0)
        _require_exact_enum(self.kind, InvocationEventKind, "kind")
        _require_utc(self.timestamp, "timestamp")


@dataclass(frozen=True, slots=True)
class LogicalInvocationResult:
    """The typed, orthogonal-field outcome of driving one logical invocation.

    `logical_invocation_id`, `role`, `state` (phase and review cycle),
    `provider_attempt`, and `agent_result` are independent dimensions that
    must agree with each other but are never collapsed into a single value
    -- later issues add Git safety and persistence status as further,
    equally orthogonal dimensions (System Design SS7.1).
    """

    logical_invocation_id: str
    role: AgentRole
    state: PipelineState
    provider_attempt: int
    agent_result: AgentResult
    events: tuple[InvocationEvent, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.logical_invocation_id, "logical_invocation_id")
        _require_exact_enum(self.role, AgentRole, "role")
        if type(self.state) is not PipelineState:
            raise TypeError("state must be PipelineState")
        if self.state.phase is not _PHASE_BY_ROLE[self.role]:
            raise ValueError("state.phase must match role")
        _require_int(self.provider_attempt, "provider_attempt", minimum=1)
        if not isinstance(self.agent_result, AgentResult):
            raise TypeError("agent_result must be AgentResult")
        if self.agent_result.role is not self.role:
            raise ValueError("agent_result role must match role")
        if self.agent_result.review_cycle != self.state.review_cycle:
            raise ValueError("agent_result review_cycle must match state.review_cycle")
        if self.agent_result.provider_attempt != self.provider_attempt:
            raise ValueError(
                "agent_result provider_attempt must match provider_attempt"
            )

        events = tuple(self.events)
        if not events:
            raise ValueError("events must not be empty")
        for index, event in enumerate(events):
            if type(event) is not InvocationEvent:
                raise TypeError("events must contain InvocationEvent")
            if event.sequence != index:
                raise ValueError("events must be sequential starting at 0")
        object.__setattr__(self, "events", events)


def _build_logical_invocation_id(
    run_id: str, role: AgentRole, review_cycle: int | None
) -> str:
    """Derive a deterministic identity shared by every attempt of one role.

    Identity is a pure function of `(run_id, role, review_cycle)`, never of
    `provider_attempt`: ADR-004 requires every retry of the same logical
    invocation to keep one identity while only `provider_attempt` advances.
    """

    cycle_component = str(review_cycle) if review_cycle is not None else "0"
    return f"{run_id}:{role.value}:{cycle_component}"


class IssueOrchestrator:
    """Drives one logical invocation of a single role via injected ports only.

    No concrete adapter is imported or constructed here: `agent_runner` and
    `run_store` are accepted as already-built `Protocol` implementations, and
    every side effect this skeleton performs goes through one of them or
    through `clock`.
    """

    def __init__(
        self,
        *,
        run_id: str,
        agent_runner: AgentRunner,
        run_store: RunStorePort,
        clock: Clock,
    ) -> None:
        _require_non_empty(run_id, "run_id")
        self._run_id = run_id
        self._agent_runner = agent_runner
        self._run_store = run_store
        self._clock = clock

    def run_logical_invocation(
        self,
        *,
        role: AgentRole,
        review_cycle: int | None,
        provider_attempt: int,
        prompt: str,
        workspace: Workspace,
    ) -> LogicalInvocationResult:
        """Run exactly one provider attempt of `role` and return its result.

        This performs only the port-facing shape of System Design SS8.3's
        mandatory sequence: determine phase/cycle/attempt, open an exclusive
        attempt sink, invoke the agent, and close the sink -- always through
        `AgentRunner`/`RunStorePort`/`Clock`. Git checkpoints, precedence
        classification, retry, and persistence are added by later issues.
        """

        _require_exact_enum(role, AgentRole, "role")
        _require_int(provider_attempt, "provider_attempt", minimum=1)
        _require_non_empty(prompt, "prompt")
        if not isinstance(workspace, Workspace):
            raise TypeError("workspace must be Workspace")

        state = PipelineState(phase=_PHASE_BY_ROLE[role], review_cycle=review_cycle)
        invocation_id = _build_logical_invocation_id(self._run_id, role, review_cycle)

        events: list[InvocationEvent] = []

        def _record(kind: InvocationEventKind) -> None:
            events.append(
                InvocationEvent(
                    sequence=len(events),
                    kind=kind,
                    timestamp=self._clock.now(),
                )
            )

        _record(InvocationEventKind.INVOCATION_STARTED)

        sink = self._run_store.open_attempt_sink(role, review_cycle, provider_attempt)
        _record(InvocationEventKind.ATTEMPT_SINK_OPENED)

        agent_result = self._agent_runner.run(
            role,
            prompt,
            workspace,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            sink=sink,
        )
        _record(InvocationEventKind.AGENT_RESULT_RECEIVED)

        sink.close()
        _record(InvocationEventKind.ATTEMPT_SINK_CLOSED)

        return LogicalInvocationResult(
            logical_invocation_id=invocation_id,
            role=role,
            state=state,
            provider_attempt=provider_attempt,
            agent_result=agent_result,
            events=tuple(events),
        )


__all__ = (
    "InvocationEvent",
    "InvocationEventKind",
    "IssueOrchestrator",
    "LogicalInvocationResult",
)

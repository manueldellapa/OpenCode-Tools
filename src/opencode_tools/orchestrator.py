"""Coordinates exactly one logical invocation of a single agent role.

`IssueOrchestrator` is the applicative coordinator described by System
Design SS5.2/SS8.3: it depends only on `domain`, `ports`, and the pure
`state_machine`/`retry`/`errors` policy modules, is constructed with already-
built ports plus a `RunRecord` snapshot handed off by bootstrap, and never
imports a concrete adapter, spawns a subprocess, parses CLI arguments or raw
protocol text, or performs any reasoning about the issue or the code (System
Design SS6; ADR-001).

`run_logical_invocation` implements System Design SS8.3's mandatory sequence
for one provider attempt: open the attempt's exclusive sink, capture a
continuous Git "before" checkpoint against the last *accepted* checkpoint
(System Design line 689 -- a delta detected here is `GIT_SAFETY_ERROR`
*before the spawn* and blocks invoking the agent at all), invoke the agent,
capture the Git "after" checkpoint unconditionally (even on a technical
failure), classify the attempt's technical outcome, decide -- but never
act on -- a provider retry (`retry.decide_retry`, M12-04's own sleep/retry
loop stays out of scope), and atomically persist a distinct, never
overwritten `AttemptRecord` for the completed attempt (System Design SS8.3
steps 8-9, SS15.4; M12-03).

`AgentRunner.run()` cannot itself call `protocol.parse_agent_response` --
its Protocol carries no `IssueLocator`, and `AgentResult`/`ProcessResult`
never carry raw process output, only sanitized digests -- so this module
never calls it either. Instead, the attempt's technical precedence is
derived from `AgentResult`'s own already-structured sub-fields
(`process.timed_out`, `provider_diagnostic` presence, `process.outcome`, and
`terminal_response`'s presence/`agent_status`), which independently
reconstructs the same `TIMEOUT > PROVIDER_ERROR > PROCESS_ERROR >
PROTOCOL_ERROR > AGENT_REPORTED_FAILURE > SUCCEEDED` precedence (System
Design SS13.2) without trusting `AgentResult.outcome` at face value and
without ever reading `terminal_response`'s semantic fields when a
higher-precedence technical signal already forbids it. A process outcome of
`LOGGING_ERROR` or `INTERRUPTED` -- neither a provider, protocol, nor
success signal -- folds into the same `process_error` signal as
`PROCESS_ERROR` for this precedence; the true, undiscarded outcome remains
available on the persisted `agent_result.process.outcome`.

Cancellation (SH-001) and a `RunStorePort.persist` failure are both handled
fail-closed and orthogonally to the technical outcome (System Design SS7.1):
once either is observed, every subsequent `run_logical_invocation` call
raises immediately, before opening a new sink, and a still-authorized
provider retry is separately suppressed by `retry.decide_retry`'s own
`cancellation_requested`/`persistence_status` guards -- so no retry is ever
authorized, and therefore no backoff sleep is ever scheduled, once
cancellation or a persistence failure has been observed. A logging failure
inside the attempt's own log sink is detected via the resulting
`AgentResult`; the affected child is already terminated by the lower-level
`ProcessRunner` (System Design SS15.5) before this module ever sees it.

Full pipeline composition, the review rework loop, bootstrap (creating the
run directory and the first `RunRecord`), and the `cli.py` composition root
(System Design SS5.3, SS8.2) remain out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    AttemptRecord,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    PersistenceStatus,
    PipelinePhase,
    ProviderRetryConfig,
    RunOutcome,
    RunRecord,
    Workspace,
)
from opencode_tools.errors import LoggingError, RunInterruptedError
from opencode_tools.ports import AgentRunner, Clock, GitSafetyPort, RunStorePort
from opencode_tools.retry import decide_retry
from opencode_tools.state_machine import (
    AttemptPrecedence,
    PipelineState,
    classify_attempt_outcome,
)

_PHASE_BY_ROLE: dict[AgentRole, PipelinePhase] = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}

_PROCESS_ERROR_LIKE_OUTCOMES = frozenset(
    {
        RunOutcome.PROCESS_ERROR,
        RunOutcome.LOGGING_ERROR,
        RunOutcome.INTERRUPTED,
    }
)


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
    """One step of this module's slice of the mandatory sequence (SS8.3)."""

    INVOCATION_STARTED = "INVOCATION_STARTED"
    ATTEMPT_SINK_OPENED = "ATTEMPT_SINK_OPENED"
    GIT_BEFORE_CHECKED = "GIT_BEFORE_CHECKED"
    BLOCKED_BY_GIT_SAFETY = "BLOCKED_BY_GIT_SAFETY"
    AGENT_RESULT_RECEIVED = "AGENT_RESULT_RECEIVED"
    GIT_AFTER_CHECKED = "GIT_AFTER_CHECKED"
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

    `git_before` is always present: every attempt, blocked or not, performs
    its continuity check. When `git_before.safety_status` is not `SAFE`, the
    agent was never invoked (System Design line 689) and `agent_result`,
    `git_after`, `precedence`, and `retry_decision` are all `None`;
    otherwise all four are present. `precedence` is this module's own
    technical classification (System Design SS13.2) and `retry_decision` is
    `retry.decide_retry`'s verdict for this attempt -- both independent of,
    and never silently reconciled with, `git_before`/`git_after`'s Git
    safety status: every dimension is reported side by side, never
    collapsed into one value (System Design SS7.1).
    """

    logical_invocation_id: str
    role: AgentRole
    state: PipelineState
    provider_attempt: int
    git_before: GitCheckRecord
    agent_result: AgentResult | None
    git_after: GitCheckRecord | None
    precedence: AttemptPrecedence | None
    retry_decision: bool | None
    events: tuple[InvocationEvent, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.logical_invocation_id, "logical_invocation_id")
        _require_exact_enum(self.role, AgentRole, "role")
        if type(self.state) is not PipelineState:
            raise TypeError("state must be PipelineState")
        if self.state.phase is not _PHASE_BY_ROLE[self.role]:
            raise ValueError("state.phase must match role")
        _require_int(self.provider_attempt, "provider_attempt", minimum=1)
        if type(self.git_before) is not GitCheckRecord:
            raise TypeError("git_before must be GitCheckRecord")

        blocked = self.git_before.safety_status is not GitSafetyStatus.SAFE
        if blocked:
            if self.agent_result is not None:
                raise ValueError("agent_result must be None when git_before is unsafe")
            if self.git_after is not None:
                raise ValueError("git_after must be None when git_before is unsafe")
            if self.precedence is not None:
                raise ValueError("precedence must be None when git_before is unsafe")
            if self.retry_decision is not None:
                raise ValueError(
                    "retry_decision must be None when git_before is unsafe"
                )
        else:
            if not isinstance(self.agent_result, AgentResult):
                raise TypeError("agent_result must be AgentResult")
            if self.agent_result.role is not self.role:
                raise ValueError("agent_result role must match role")
            if self.agent_result.review_cycle != self.state.review_cycle:
                raise ValueError(
                    "agent_result review_cycle must match state.review_cycle"
                )
            if self.agent_result.provider_attempt != self.provider_attempt:
                raise ValueError(
                    "agent_result provider_attempt must match provider_attempt"
                )
            if type(self.git_after) is not GitCheckRecord:
                raise TypeError("git_after must be GitCheckRecord")
            if type(self.precedence) is not AttemptPrecedence:
                raise TypeError("precedence must be AttemptPrecedence")
            if type(self.retry_decision) is not bool:
                raise TypeError("retry_decision must be a bool")

        events = tuple(self.events)
        if not events:
            raise ValueError("events must not be empty")
        for index, event in enumerate(events):
            if type(event) is not InvocationEvent:
                raise TypeError("events must contain InvocationEvent")
            if event.sequence != index:
                raise ValueError("events must be sequential starting at 0")
        object.__setattr__(self, "events", events)


def _cycle_component(review_cycle: int | None) -> str:
    return str(review_cycle) if review_cycle is not None else "0"


def _build_logical_invocation_id(
    run_id: str, role: AgentRole, review_cycle: int | None
) -> str:
    """Derive a deterministic identity shared by every attempt of one role.

    Identity is a pure function of `(run_id, role, review_cycle)`, never of
    `provider_attempt`: ADR-004 requires every retry of the same logical
    invocation to keep one identity while only `provider_attempt` advances.
    """

    return f"{run_id}:{role.value}:{_cycle_component(review_cycle)}"


def _classify_agent_result(agent_result: AgentResult) -> AttemptPrecedence:
    """Independently reconstruct the attempt's precedence from raw signals.

    Never trusts `agent_result.outcome` directly: each boolean is derived
    from a structural sub-field (`process.timed_out`, `provider_diagnostic`
    presence, `process.outcome`, `terminal_response`'s presence and
    `agent_status`), matching System Design SS13.2's precedence. A
    `process.outcome` of `LOGGING_ERROR` or `INTERRUPTED` is folded into the
    same `process_error` signal as `PROCESS_ERROR`: System Design SS13.2's
    six-outcome precedence has no separate category for either, both are
    non-provider/non-protocol technical failures that never retry, and the
    true outcome stays available, undiscarded, on the persisted
    `agent_result.process.outcome`. Only when none of
    `timed_out`/`provider_error`/`process_error` holds does this function
    look at `terminal_response` at all -- so a `terminal_response` left on
    the result by a misbehaving caller is never consulted once a
    higher-precedence technical signal already applies (AC-016/AC-017).
    """

    timed_out = agent_result.process.timed_out
    provider_error = agent_result.provider_diagnostic is not None
    process_error = agent_result.process.outcome in _PROCESS_ERROR_LIKE_OUTCOMES
    higher_precedence_signal = timed_out or provider_error or process_error

    protocol_error = (
        not higher_precedence_signal and agent_result.terminal_response is None
    )
    agent_reported_failure = (
        not higher_precedence_signal
        and agent_result.terminal_response is not None
        and agent_result.terminal_response.agent_status is AgentStatus.FAILED
    )
    succeeded = (
        not higher_precedence_signal
        and agent_result.terminal_response is not None
        and agent_result.terminal_response.agent_status is not AgentStatus.FAILED
    )

    return classify_attempt_outcome(
        timed_out=timed_out,
        provider_error=provider_error,
        process_error=process_error,
        protocol_error=protocol_error,
        agent_reported_failure=agent_reported_failure,
        succeeded=succeeded,
    )


class IssueOrchestrator:
    """Drives logical invocations of a single role via injected ports only.

    No concrete adapter is imported or constructed here: every port is
    accepted as an already-built `Protocol` implementation. `initial_record`
    is the `RunRecord` snapshot bootstrap already persisted before this
    module ever runs (System Design SS8.2 step 5, out of scope here); this
    module only ever *evolves* it, via `dataclasses.replace`, into a new
    immutable snapshot after each completed attempt (System Design SS15.4)
    and hands each one to `run_store.persist`. `target`, `run_id`, and the
    Git continuity baseline (`initial_record.git_baseline`, `None` until
    bootstrap has captured one) are all derived from `initial_record` rather
    than repeated as separate constructor arguments.

    Once a `persist` call reports anything other than `PersistenceStatus.OK`,
    or `open_attempt_sink` itself raises `LoggingError`, every subsequent
    `run_logical_invocation` call raises `LoggingError` immediately, before
    doing anything else (System Design SS15.4: "interrompe nuove
    invocation"); `self._record` then simply stops advancing, so the last
    *successfully* persisted snapshot -- itself possibly partial -- remains
    the authoritative one, exactly as the real atomic-replace adapter leaves
    the previous `run.json` in place on a failed write. `request_cancellation`
    gives an external SIGINT/SIGTERM handler (not yet built; CLI/M14 scope)
    a way to signal the same fail-closed behavior; an `AgentResult` whose
    `process.outcome` is `INTERRUPTED` sets the same flag as a matter of
    course, since a child only reports `INTERRUPTED` after a caught signal.
    """

    def __init__(
        self,
        *,
        initial_record: RunRecord,
        agent_runner: AgentRunner,
        run_store: RunStorePort,
        git_safety: GitSafetyPort,
        clock: Clock,
        provider_retry: ProviderRetryConfig,
    ) -> None:
        if type(initial_record) is not RunRecord:
            raise TypeError("initial_record must be RunRecord")
        if type(provider_retry) is not ProviderRetryConfig:
            raise TypeError("provider_retry must be ProviderRetryConfig")

        self._record = initial_record
        self._run_id = initial_record.run_id
        self._target = initial_record.target
        self._agent_runner = agent_runner
        self._run_store = run_store
        self._git_safety = git_safety
        self._clock = clock
        self._provider_retry = provider_retry
        self._last_accepted_git_state: GitState | None = initial_record.git_baseline
        used_sequences = [check.sequence for check in initial_record.git_checks]
        self._next_git_sequence = max(used_sequences, default=-1) + 1
        self._persistence_blocked = (
            initial_record.persistence_status is not PersistenceStatus.OK
        )
        self._cancellation_requested = False

    def request_cancellation(self) -> None:
        """Record an external cancellation request (System Design SH-001).

        Idempotent. Every subsequent `run_logical_invocation` call then
        raises `RunInterruptedError` immediately, before opening a sink (the
        "prima"/idle case); a still-running attempt is unaffected by this
        call alone (the "durante"/active case is instead driven by the
        child's own `INTERRUPTED` process outcome), and any later attempt
        that would otherwise authorize a provider retry has that retry
        suppressed by `retry.decide_retry`'s own `cancellation_requested`
        guard, so no backoff sleep is ever scheduled either (the
        "nel sleep" case).
        """

        self._cancellation_requested = True

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

        Follows System Design SS8.3's order exactly: fail fast on a prior
        persistence failure or cancellation request, open the exclusive
        sink, take the continuity "before" checkpoint (`role=None`, against
        the last *accepted* checkpoint), and -- only if that checkpoint is
        `SAFE` -- invoke the agent, take the "after" checkpoint (`role`'s own
        tolerance applies, unconditionally, even on a technical failure),
        classify the attempt, decide (but never act on) a provider retry,
        and atomically persist a new, distinct `AttemptRecord` for it. A
        `before` that is not `SAFE` blocks the agent invocation entirely
        (System Design line 689), is reported with `agent_result`/
        `git_after`/`precedence`/`retry_decision` all `None`, and is never
        persisted here -- pipeline-level Git-safety finalization is M13's
        concern.
        """

        _require_exact_enum(role, AgentRole, "role")
        _require_int(provider_attempt, "provider_attempt", minimum=1)
        _require_non_empty(prompt, "prompt")
        if not isinstance(workspace, Workspace):
            raise TypeError("workspace must be Workspace")

        if self._persistence_blocked:
            raise LoggingError(
                code="orchestrator.persistence_blocked",
                message="A prior persistence failure blocks new invocations.",
                related_record=self._run_id,
            )
        if self._cancellation_requested:
            raise RunInterruptedError(
                code="orchestrator.cancellation_requested",
                message="A cancellation request blocks new invocations.",
                related_record=self._run_id,
            )

        state = PipelineState(phase=_PHASE_BY_ROLE[role], review_cycle=review_cycle)
        invocation_id = _build_logical_invocation_id(self._run_id, role, review_cycle)
        cycle_component = _cycle_component(review_cycle)

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

        try:
            sink = self._run_store.open_attempt_sink(
                role, review_cycle, provider_attempt
            )
        except LoggingError:
            self._persistence_blocked = True
            raise
        _record(InvocationEventKind.ATTEMPT_SINK_OPENED)

        before = self._git_safety.check(
            self._target,
            sequence=self._next_git_sequence,
            purpose=f"{role.value}:{cycle_component}:{provider_attempt}:before",
            role=None,
            baseline=self._last_accepted_git_state,
        )
        self._next_git_sequence += 1
        _record(InvocationEventKind.GIT_BEFORE_CHECKED)

        if before.safety_status is not GitSafetyStatus.SAFE:
            sink.close()
            _record(InvocationEventKind.ATTEMPT_SINK_CLOSED)
            _record(InvocationEventKind.BLOCKED_BY_GIT_SAFETY)
            return LogicalInvocationResult(
                logical_invocation_id=invocation_id,
                role=role,
                state=state,
                provider_attempt=provider_attempt,
                git_before=before,
                agent_result=None,
                git_after=None,
                precedence=None,
                retry_decision=None,
                events=tuple(events),
            )

        agent_result = self._agent_runner.run(
            role,
            prompt,
            workspace,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            sink=sink,
        )
        _record(InvocationEventKind.AGENT_RESULT_RECEIVED)

        if agent_result.process.outcome is RunOutcome.INTERRUPTED:
            self._cancellation_requested = True
        if agent_result.process.outcome is RunOutcome.LOGGING_ERROR:
            self._persistence_blocked = True

        after = self._git_safety.check(
            self._target,
            sequence=self._next_git_sequence,
            purpose=f"{role.value}:{cycle_component}:{provider_attempt}:after",
            role=role,
            baseline=before.state,
        )
        self._next_git_sequence += 1
        _record(InvocationEventKind.GIT_AFTER_CHECKED)
        if after.safety_status is GitSafetyStatus.SAFE:
            self._last_accepted_git_state = after.state

        sink.close()
        _record(InvocationEventKind.ATTEMPT_SINK_CLOSED)

        precedence = _classify_agent_result(agent_result)

        retry_decision = decide_retry(
            outcome=precedence.outcome,
            provider_diagnostic=agent_result.provider_diagnostic,
            provider_attempt=provider_attempt,
            role=role,
            target_changed=before.state.fingerprint != after.state.fingerprint,
            termination_confirmed=agent_result.process.termination_confirmed,
            git_safety_status=after.safety_status,
            persistence_status=self._record.persistence_status,
            cancellation_requested=self._cancellation_requested,
            config=self._provider_retry,
        )

        attempt_record = AttemptRecord(
            logical_invocation_id=invocation_id,
            role=role,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            git_before=before,
            git_after=after,
            agent_result=agent_result,
            retry_decision=retry_decision.should_retry,
        )

        updated_record = replace(
            self._record,
            attempts=(*self._record.attempts, attempt_record),
            current_phase=state.phase,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
        )
        status = self._run_store.persist(updated_record)
        if status is PersistenceStatus.OK:
            self._record = updated_record
        else:
            self._persistence_blocked = True
            raise LoggingError(
                code="orchestrator.run_record_persist_failed",
                message="Failed to persist run.json after a completed attempt.",
                related_record=invocation_id,
            )

        return LogicalInvocationResult(
            logical_invocation_id=invocation_id,
            role=role,
            state=state,
            provider_attempt=provider_attempt,
            git_before=before,
            agent_result=agent_result,
            git_after=after,
            precedence=precedence,
            retry_decision=retry_decision.should_retry,
            events=tuple(events),
        )


__all__ = (
    "InvocationEvent",
    "InvocationEventKind",
    "IssueOrchestrator",
    "LogicalInvocationResult",
)

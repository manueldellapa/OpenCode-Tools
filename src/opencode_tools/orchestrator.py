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
*before the spawn* and blocks invoking the agent at all), reconfirm the
control-plane digest `OpenCodePreflightPort.verify` established at bootstrap
is still unchanged (`OpenCodePreflightPort.recheck`; System Design SS18.2;
ADR-005 -- a drift here likewise blocks the spawn, closing a gap M12-02
originally left open), invoke the agent, capture the Git "after" checkpoint
unconditionally (even on a technical failure), classify the attempt's
technical outcome, decide a provider retry (`retry.decide_retry`), and
atomically persist a distinct, never overwritten `AttemptRecord` for the
completed attempt (System Design SS8.3 steps 8-9, SS15.4; M12-03).
`run_provider_attempts` is the thin loop around it that
*acts* on that verdict (System Design SS12.2; M12-04): it sleeps the exact,
already-capped backoff via the injected `Sleeper` and re-invokes with the
next `provider_attempt` only while `decide_retry` keeps authorizing it,
stopping -- with `provider_attempt`'s budget spent exactly, never over- or
under-shot, and the review cycle untouched -- the moment it does not,
whatever the reason (a non-`PROVIDER_ERROR` outcome, an untrusted or
non-retryable diagnostic, attempt-budget exhaustion, unconfirmed
termination, `UNSAFE`/`INDETERMINATE` Git, a coder's own target fingerprint
having changed, a persistence failure, or a cancellation request). Which
phase to transition to next, whether a review cycle should advance, and
final status/gate decisions are still not this module's concern (System
Design SS8.1/SS8.4, SS13.3; M13).

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
raises immediately, before opening a new sink -- so `run_provider_attempts`
never sleeps and never starts a next attempt either, the exception simply
propagating out of its loop -- and a still-authorized provider retry is
separately suppressed by `retry.decide_retry`'s own
`cancellation_requested`/`persistence_status` guards for a cancellation or
write fault observed *during* an attempt that itself still completed and
persisted normally. A logging failure inside the attempt's own log sink is
detected via the resulting `AgentResult`; the affected child is already
terminated by the lower-level `ProcessRunner` (System Design SS15.5) before
this module ever sees it.

`bootstrap_run` composes System Design SS8.2 steps 3-6 of the lifecycle,
strictly ahead of any role invocation (M13-01): the read-only runtime
location check, target resolution, acquiring the target-scoped lease
*before* the baseline (System Design SS16.2; ADR-006), creating the run
directory and persisting the first `RunRecord`, capturing the Git baseline,
resolving a single `IssueLocator`, verifying OpenCode's own exact-version
compatibility/capability/effective-agent/control-plane preflight exactly
once (System Design SS10.4; ADR-005), and -- only once every one of those
succeeds -- constructing the `IssueOrchestrator` this module already
provides, handing it the canonical control-plane digest `verify` returned
so every later provider attempt can reconfirm it (System Design SS18.2).
Every step is driven through the already-injected `GitSafetyPort`/
`IssueResolver`/`RunStorePort`/`TargetLeaseFactory`/`OpenCodePreflightPort`
ports (never a concrete adapter, never `opencode.py` itself):
`OpenCodePreflightPort` mirrors `IssueResolver` in keeping every other
OpenCode-specific fact -- the parsed version, the raw `debug` evidence --
behind the concrete adapter's own boundary; only the digest itself crosses,
exactly as `AgentRunner` keeps the actual role invocation out of scope here
(M13-02). A failure at any bootstrap/preflight step returns a typed
`BootstrapOutcome` with `orchestrator=None`: no agent role is ever invoked.
The acquired `TargetLease`, when there is one, is always handed back on the
outcome, on success or failure alike, and is never released or quarantined
by this function -- it is held "from before the baseline until after
finalization" (System Design SS16.2), and finalization is M13-04's concern,
not this one's.

Full pipeline composition (driving the state machine through the architect/
coder/reviewer roles), the review rework loop, postflight/finalization, and
the `cli.py` composition root (System Design SS5.3, SS8.2) remain out of
scope here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    AppConfig,
    AttemptRecord,
    FrozenJsonValue,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    IssueLocator,
    PersistenceStatus,
    PipelinePhase,
    ProviderRetryConfig,
    RetryDecision,
    RunOutcome,
    RunRecord,
    RunRequest,
    Workspace,
)
from opencode_tools.errors import (
    GitSafetyError,
    LoggingError,
    OpenCodeToolsError,
    ProtocolError,
    RunInterruptedError,
    to_error_records,
)
from opencode_tools.ports import (
    AgentRunner,
    Clock,
    GitSafetyPort,
    IssueResolver,
    OpenCodePreflightPort,
    RunStorePort,
    Sleeper,
    TargetLease,
    TargetLeaseFactory,
)
from opencode_tools.retry import decide_retry
from opencode_tools.state_machine import (
    AttemptPrecedence,
    PipelineState,
    TransitionEvent,
    TransitionEventKind,
    classify_attempt_outcome,
    transition,
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
    CONTROL_PLANE_RECHECKED = "CONTROL_PLANE_RECHECKED"
    BLOCKED_BY_CONTROL_PLANE_DRIFT = "BLOCKED_BY_CONTROL_PLANE_DRIFT"
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
    its continuity check. The agent is blocked from ever being invoked
    (System Design line 689; SS18.2) whenever `git_before.safety_status` is
    not `SAFE`, or -- only ever checked once `git_before` *is* `SAFE` --
    `control_plane_error` is set, meaning `OpenCodePreflightPort.recheck`
    detected a control-plane drift; the two block reasons are mutually
    exclusive, since a drifted `before` never reaches the recheck at all.
    Either way, `agent_result`, `git_after`, `precedence`, and
    `retry_decision` are all `None`; otherwise (neither block reason holds)
    all four are present and `control_plane_error` is `None`. `precedence`
    is this module's own technical classification (System Design SS13.2)
    and `retry_decision` is `retry.decide_retry`'s full verdict for this
    attempt -- including the exact, already-capped `planned_delay_seconds`
    `run_provider_attempts` sleeps before the next attempt -- both
    independent of, and never silently reconciled with, `git_before`/
    `git_after`'s Git safety status or `control_plane_error`: every
    dimension is reported side by side, never collapsed into one value
    (System Design SS7.1).
    """

    logical_invocation_id: str
    role: AgentRole
    state: PipelineState
    provider_attempt: int
    git_before: GitCheckRecord
    control_plane_error: OpenCodeToolsError | None
    agent_result: AgentResult | None
    git_after: GitCheckRecord | None
    precedence: AttemptPrecedence | None
    retry_decision: RetryDecision | None
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
        if self.control_plane_error is not None and not isinstance(
            self.control_plane_error, OpenCodeToolsError
        ):
            raise TypeError("control_plane_error must be OpenCodeToolsError or None")

        blocked_by_git = self.git_before.safety_status is not GitSafetyStatus.SAFE
        blocked_by_control_plane = self.control_plane_error is not None
        if blocked_by_git and blocked_by_control_plane:
            raise ValueError(
                "control_plane_error must be None when git_before is unsafe"
            )
        blocked = blocked_by_git or blocked_by_control_plane
        if blocked:
            if self.agent_result is not None:
                raise ValueError("agent_result must be None when blocked")
            if self.git_after is not None:
                raise ValueError("git_after must be None when blocked")
            if self.precedence is not None:
                raise ValueError("precedence must be None when blocked")
            if self.retry_decision is not None:
                raise ValueError("retry_decision must be None when blocked")
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
            if type(self.retry_decision) is not RetryDecision:
                raise TypeError("retry_decision must be RetryDecision")

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
    `sleeper` is only ever driven by `run_provider_attempts`, with the exact
    `planned_delay_seconds` `retry.decide_retry` already computed -- never a
    real sleep in this module's own tests.

    `control_plane_digest` is the canonical digest `bootstrap_run` already
    obtained from one `OpenCodePreflightPort.verify()` call (M13-01); this
    class never calls `verify()` itself -- only `recheck(control_plane_digest)`,
    once per provider attempt, always with this same, never-recomputed
    value (System Design SS18.2; ADR-005; closing a gap M12-02/#45 left
    open).
    """

    def __init__(
        self,
        *,
        initial_record: RunRecord,
        agent_runner: AgentRunner,
        run_store: RunStorePort,
        git_safety: GitSafetyPort,
        opencode_preflight: OpenCodePreflightPort,
        control_plane_digest: str,
        clock: Clock,
        sleeper: Sleeper,
        provider_retry: ProviderRetryConfig,
    ) -> None:
        if type(initial_record) is not RunRecord:
            raise TypeError("initial_record must be RunRecord")
        if type(provider_retry) is not ProviderRetryConfig:
            raise TypeError("provider_retry must be ProviderRetryConfig")
        _require_non_empty(control_plane_digest, "control_plane_digest")

        self._record = initial_record
        self._run_id = initial_record.run_id
        self._target = initial_record.target
        self._agent_runner = agent_runner
        self._run_store = run_store
        self._git_safety = git_safety
        self._opencode_preflight = opencode_preflight
        self._control_plane_digest = control_plane_digest
        self._clock = clock
        self._sleeper = sleeper
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
        `SAFE` -- reconfirm the control-plane digest
        (`OpenCodePreflightPort.recheck`; System Design SS18.2), and -- only
        if that also still matches -- invoke the agent, take the "after"
        checkpoint (`role`'s own tolerance applies, unconditionally, even on
        a technical failure), classify the attempt, decide (but never act
        on) a provider retry, and atomically persist a new, distinct
        `AttemptRecord` for it. A `before` that is not `SAFE`, or a
        control-plane digest that has drifted, each block the agent
        invocation entirely (System Design line 689; SS18.2), are reported
        with `agent_result`/`git_after`/`precedence`/`retry_decision` all
        `None`, and are never persisted here -- pipeline-level Git-safety/
        control-plane finalization is M13's concern.
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
                control_plane_error=None,
                agent_result=None,
                git_after=None,
                precedence=None,
                retry_decision=None,
                events=tuple(events),
            )

        try:
            self._opencode_preflight.recheck(self._control_plane_digest)
        except ProtocolError as error:
            sink.close()
            _record(InvocationEventKind.ATTEMPT_SINK_CLOSED)
            _record(InvocationEventKind.BLOCKED_BY_CONTROL_PLANE_DRIFT)
            return LogicalInvocationResult(
                logical_invocation_id=invocation_id,
                role=role,
                state=state,
                provider_attempt=provider_attempt,
                git_before=before,
                control_plane_error=error,
                agent_result=None,
                git_after=None,
                precedence=None,
                retry_decision=None,
                events=tuple(events),
            )
        _record(InvocationEventKind.CONTROL_PLANE_RECHECKED)

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
            control_plane_error=None,
            agent_result=agent_result,
            git_after=after,
            precedence=precedence,
            retry_decision=retry_decision,
            events=tuple(events),
        )

    def run_provider_attempts(
        self,
        *,
        role: AgentRole,
        review_cycle: int | None,
        prompt: str,
        workspace: Workspace,
    ) -> tuple[LogicalInvocationResult, ...]:
        """Run every provider attempt of one logical invocation (SS12.2).

        Starts at `provider_attempt=1` and repeatedly calls
        `run_logical_invocation`, sleeping via the injected `Sleeper` for
        exactly `retry_decision.planned_delay_seconds` between attempts, for
        as long as -- and only as long as -- each attempt's own
        `retry_decision.should_retry` authorizes another one. That verdict
        already applies every guard (System Design SS12.2): a trusted,
        still-budgeted `PROVIDER_ERROR`, confirmed termination, `SAFE` Git,
        `OK` persistence, no cancellation, and -- for a coder -- an
        unchanged target fingerprint. Attempt-budget exhaustion needs no
        special handling here: `retry.decide_retry` simply stops authorizing
        once `provider_attempt` reaches `provider_retry.max_attempts`, so
        the loop ends having spent exactly that budget, the attempt's own
        `PROVIDER_ERROR` precedence unchanged, and `review_cycle` never
        touched (a provider retry never advances it; ADR-004). A blocked
        invocation (git-before unsafe, or a control-plane digest drift caught
        by `OpenCodePreflightPort.recheck`) or a raised `LoggingError`/
        `RunInterruptedError` also ends the loop -- the latter two simply by
        propagating, before any sleep. Returns every attempt actually run,
        in order; deciding what happens next (a phase transition, a new
        review cycle, final status) is still not this module's concern
        (M13).
        """

        results: list[LogicalInvocationResult] = []
        provider_attempt = 1
        while True:
            result = self.run_logical_invocation(
                role=role,
                review_cycle=review_cycle,
                provider_attempt=provider_attempt,
                prompt=prompt,
                workspace=workspace,
            )
            results.append(result)

            decision = result.retry_decision
            if decision is None or not decision.should_retry:
                return tuple(results)

            next_attempt = decision.next_provider_attempt
            delay = decision.planned_delay_seconds
            if next_attempt is None or delay is None:
                raise AssertionError(
                    "RetryDecision.should_retry implies both "
                    "next_provider_attempt and planned_delay_seconds are set"
                )
            self._sleeper.sleep(delay)
            provider_attempt = next_attempt


_BASELINE_CHECK_PURPOSE = "baseline"


@dataclass(frozen=True, slots=True)
class BootstrapOutcome:
    """The typed result of `bootstrap_run` (System Design SS8.2 steps 3-6).

    `lease` is present whenever `TargetLeaseFactory.acquire` succeeded --
    on a full success and on any later-stage failure alike -- so the
    caller can keep holding it through postflight/finalization (System
    Design SS16.2; out of scope here) or release/quarantine it; it is
    `None` only when acquisition itself never ran or itself failed.
    `orchestrator` and `issue_locator` are present if and only if `error`
    is `None`: every bootstrap/preflight step succeeded, and this call has
    not invoked -- and will never invoke -- any agent role itself (System
    Design SS8.2: "nessun agent parte su preflight non verde"). `record` is
    the last `RunRecord` snapshot this call itself durably persisted: the
    fully preflighted one on success, a best-effort failure snapshot when a
    run directory already existed to update, or `None` when the failure
    happened before one could ever be created (System Design SS8.2 point 3:
    these errors "possono precedere run.json").
    """

    lease: TargetLease | None
    error: OpenCodeToolsError | None
    orchestrator: IssueOrchestrator | None
    issue_locator: IssueLocator | None
    record: RunRecord | None

    def __post_init__(self) -> None:
        if self.error is None:
            if not isinstance(self.orchestrator, IssueOrchestrator):
                raise TypeError(
                    "orchestrator must be IssueOrchestrator when error is None"
                )
            if not isinstance(self.issue_locator, IssueLocator):
                raise TypeError("issue_locator must be IssueLocator when error is None")
            if type(self.record) is not RunRecord:
                raise TypeError("record must be RunRecord when error is None")
        else:
            if not isinstance(self.error, OpenCodeToolsError):
                raise TypeError("error must be OpenCodeToolsError or None")
            if self.orchestrator is not None:
                raise ValueError("orchestrator must be None when error is set")
            if self.issue_locator is not None:
                raise ValueError("issue_locator must be None when error is set")
            if self.record is not None and type(self.record) is not RunRecord:
                raise TypeError("record must be RunRecord or None")


def _persist_or_raise(run_store: RunStorePort, record: RunRecord) -> None:
    status = run_store.persist(record)
    if status is not PersistenceStatus.OK:
        raise LoggingError(
            "orchestrator.bootstrap_persist_failed",
            "Failed to persist run.json during bootstrap.",
            related_record=record.run_id,
        )


def _bootstrap_failure(
    *, lease: TargetLease | None, error: OpenCodeToolsError, record: RunRecord | None
) -> BootstrapOutcome:
    return BootstrapOutcome(
        lease=lease, error=error, orchestrator=None, issue_locator=None, record=record
    )


def bootstrap_run(
    *,
    run_request: RunRequest,
    config: AppConfig,
    run_id: str,
    config_snapshot: Mapping[str, FrozenJsonValue],
    environment_snapshot: Mapping[str, FrozenJsonValue],
    git_safety: GitSafetyPort,
    issue_resolver: IssueResolver,
    run_store: RunStorePort,
    lease_factory: TargetLeaseFactory,
    opencode_preflight: OpenCodePreflightPort,
    agent_runner: AgentRunner,
    clock: Clock,
    sleeper: Sleeper,
) -> BootstrapOutcome:
    """Compose bootstrap, preflight, baseline capture, and the lease (SS8.2).

    Follows System Design SS8.2's binding order for everything that must
    happen before the architect can ever be invoked:

    1. the read-only runtime-location check (`GitSafetyPort.
       check_runtime_location`) and target resolution (`GitSafetyPort.
       resolve_target`, which itself proves containment, Git top-level,
       attached branch, and a clean working tree);
    2. acquiring the target-scoped lease (`TargetLeaseFactory.acquire`),
       strictly *before* the baseline capture (System Design SS16.2;
       ADR-006) -- every failure from here on keeps whatever lease was
       already acquired, on `BootstrapOutcome.lease`, for the caller to
       hold or release; this function never releases or quarantines it
       itself;
    3. creating the run directory and persisting the first, minimal
       `RunRecord` (`RunStorePort.initialize`/`persist`);
    4. capturing the Git baseline checkpoint (`GitSafetyPort.check` with
       `role=None`, `baseline=None`) -- recorded on `git_checks` whatever
       its status; an `INDETERMINATE` capture fails closed with
       `GitSafetyError` rather than ever being treated as clean (System
       Design AC-036) and never becomes `git_baseline`; it can never be
       `UNSAFE` here, since there is nothing yet to compare it against;
    5. resolving exactly one `IssueLocator` from the target (`IssueResolver.
       resolve_repository`/`locate_issue`) -- Python never reads the issue
       body;
    6. verifying OpenCode's own exact-version compatibility, `run`
       capability, effective per-role agent configuration, and
       control-plane digest, exactly once (`OpenCodePreflightPort.verify`;
       System Design SS10.4; ADR-005) -- the last preflight fact gathered,
       immediately before the pipeline is declared ready for the architect;
    7. driving `state_machine.transition` from `PREFLIGHT`, and -- only on
       full success -- constructing the `IssueOrchestrator` this module
       already provides, from the now-fully-preflighted `RunRecord`.

    A failure raised by any port call at steps 3-6 (always an
    `OpenCodeToolsError` subclass) is caught, folded into a `PREFLIGHT`-
    phase `TransitionEvent(TERMINAL_OUTCOME)` to compute the correct
    terminal phase, and -- whenever a run directory already exists to
    update -- best-effort persisted as an amended `RunRecord` that carries
    forward every fact steps 3-6 already established *before* the failure
    (the run directory, and whichever of the Git baseline checkpoint or
    `IssueLocator` were already resolved), plus the error's
    `to_error_records()`; a second, unrelated persistence failure at that
    point is not allowed to mask the original error, so it is swallowed
    rather than raised, and the outcome falls back to the last snapshot
    genuinely known to be durable (System Design SS15.4: the last valid
    `run.json` may stay partial). Every returned `BootstrapOutcome` on this
    path has `orchestrator=None`: no agent role has been, or ever will be,
    invoked by this function -- step 6's own failure is no exception, since
    `OpenCodePreflightPort.verify` is called directly by this function, not
    through `AgentRunner`.

    `OpenCodePreflightPort` deliberately carries no OpenCode-specific
    value across this boundary -- no parsed version, no control-plane
    digest -- mirroring `IssueResolver`: the concrete adapter keeps that
    evidence behind its own construction, exactly as `github.
    GhPreflightEvidence` never crosses `IssueResolver.locate_issue`'s
    return type either. `agent_runner` is accepted here only to be
    threaded, unused, into the `IssueOrchestrator` this function
    constructs on success; role execution itself stays out of this
    function's scope (M13-02).
    """

    if not isinstance(run_request, RunRequest):
        raise TypeError("run_request must be RunRequest")
    if not isinstance(config, AppConfig):
        raise TypeError("config must be AppConfig")
    _require_non_empty(run_id, "run_id")

    workspace = run_request.workspace
    runtime_root = config.runtime_root

    try:
        git_safety.check_runtime_location(runtime_root)
        target = git_safety.resolve_target(workspace, run_request.target_root)
    except OpenCodeToolsError as error:
        return _bootstrap_failure(lease=None, error=error, record=None)

    try:
        lease = lease_factory.acquire(target.root, runtime_root, run_id)
    except OpenCodeToolsError as error:
        return _bootstrap_failure(lease=None, error=error, record=None)

    # From here on the lease is held; every failure below still hands it
    # back on the outcome. `latest_record` accumulates every fact already
    # established -- the run directory, the Git baseline check, the
    # `IssueLocator` -- as soon as each is known, so a later-stage failure
    # never discards evidence an earlier stage already durably produced.
    # `persisted_record` tracks the last snapshot genuinely known to be on
    # disk, the fallback if even the best-effort failure amend can't be
    # written either.
    latest_record: RunRecord | None = None
    persisted_record: RunRecord | None = None

    try:
        run_directory = run_store.initialize(workspace, run_id)
        latest_record = RunRecord(
            schema_version=1,
            run_id=run_id,
            artifact_path=run_directory / "run.json",
            workspace=workspace,
            target=target,
            issue_number=run_request.issue_number,
            config=config_snapshot,
            environment=environment_snapshot,
            started_at=clock.now(),
            current_phase=PipelinePhase.PREFLIGHT,
            persistence_status=PersistenceStatus.OK,
        )
        _persist_or_raise(run_store, latest_record)
        persisted_record = latest_record

        baseline_check = git_safety.check(
            target,
            sequence=0,
            purpose=_BASELINE_CHECK_PURPOSE,
            role=None,
            baseline=None,
        )
        latest_record = replace(latest_record, git_checks=(baseline_check,))
        if baseline_check.safety_status is not GitSafetyStatus.SAFE:
            raise GitSafetyError(
                "orchestrator.baseline_not_safe",
                "The Git baseline capture was not SAFE.",
                related_record=run_id,
            )
        latest_record = replace(
            latest_record,
            git_baseline=baseline_check.state,
            git_safety_status=GitSafetyStatus.SAFE,
        )

        repository_identity = issue_resolver.resolve_repository(target)
        issue_locator = issue_resolver.locate_issue(
            repository_identity, run_request.issue_number
        )
        latest_record = replace(latest_record, issue_locator=issue_locator)

        control_plane_digest = opencode_preflight.verify()

        ready_transition = transition(
            PipelineState(phase=PipelinePhase.PREFLIGHT),
            TransitionEvent(kind=TransitionEventKind.PREFLIGHT_SUCCEEDED),
            max_review_cycles=config.execution.max_review_cycles,
        )
        ready_record = replace(
            latest_record, current_phase=ready_transition.state.phase
        )
        _persist_or_raise(run_store, ready_record)
    except OpenCodeToolsError as error:
        if latest_record is None:
            return _bootstrap_failure(lease=lease, error=error, record=None)

        failure_transition = transition(
            PipelineState(phase=PipelinePhase.PREFLIGHT),
            TransitionEvent(
                kind=TransitionEventKind.TERMINAL_OUTCOME, outcome=error.outcome
            ),
            max_review_cycles=config.execution.max_review_cycles,
        )
        failure_record = replace(
            latest_record,
            current_phase=failure_transition.state.phase,
            errors=to_error_records(
                error,
                phase=PipelinePhase.PREFLIGHT,
                timestamp=clock.now(),
                first_sequence=0,
            ),
        )
        try:
            _persist_or_raise(run_store, failure_record)
            persisted_record = failure_record
        except OpenCodeToolsError:
            pass
        return _bootstrap_failure(lease=lease, error=error, record=persisted_record)

    orchestrator = IssueOrchestrator(
        initial_record=ready_record,
        agent_runner=agent_runner,
        run_store=run_store,
        git_safety=git_safety,
        opencode_preflight=opencode_preflight,
        control_plane_digest=control_plane_digest,
        clock=clock,
        sleeper=sleeper,
        provider_retry=config.provider_retry,
    )
    return BootstrapOutcome(
        lease=lease,
        error=None,
        orchestrator=orchestrator,
        issue_locator=issue_locator,
        record=ready_record,
    )


__all__ = (
    "BootstrapOutcome",
    "InvocationEvent",
    "InvocationEventKind",
    "IssueOrchestrator",
    "LogicalInvocationResult",
    "bootstrap_run",
)

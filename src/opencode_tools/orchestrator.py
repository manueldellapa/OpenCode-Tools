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

`run_issue_pipeline` composes the full nominal-through-rework path --
`architect -> coder(1) -> reviewer(1) -> [coder(n+1) -> reviewer(n+1)]*`
(System Design SS8.1, SS9, SS12.3; M13-02, M13-03): it invokes each role, in
turn, as its own primary-agent session through `IssueOrchestrator.
run_provider_attempts` (never a fourth agent, never a raw `AgentRunner.run`
call bypassing checkpoint/persistence, and never any new provider-retry
logic of its own -- every attempt within one logical invocation is still
M12's `run_provider_attempts` loop, unchanged), and reconstructs the
resulting `state_machine.TransitionEvent` independently of
`AgentResult.outcome`, exactly as `_classify_agent_result` already does for
a single attempt -- never trusting an upstream `AgentRunner` adapter's own
claimed `AgentStatus`/`ReviewStatus` without also requiring the attempt's
own Git "after" checkpoint stayed `SAFE` (System Design SS7.1: protocol
precedence and Git safety are orthogonal, and a mutation by the read-only
architect or reviewer must block the next phase exactly as a protocol
failure would, never silently excused because the marker itself parsed).
Architect's opaque handoff body and discovered `IssueRef`, the coder's
opaque report alongside its own final Git change inventory
(`GitState.staged/unstaged/untracked`, never a diff), and -- from review
cycle 2 onward -- the reviewer's own opaque `CHANGES_REQUIRED` body, are
transported into the next role's prompt via `prompting.py` without Python
ever reading, summarizing, or judging them (ADR-002, ADR-010).

The coder is never invoked without a `READY` architect response carrying a
locator-consistent `IssueRef`; the reviewer is never invoked without a
`COMPLETED` coder response. A reviewer `CHANGES_REQUIRED` at a review cycle
still below `max_review_cycles` opens the next coder(n+1)/reviewer(n+1)
cycle -- `review_cycle` and `provider_attempt` remain the separate counters
System Design SS12.1/SS12.3 requires, so a provider retry inside any single
role invocation never advances `review_cycle` and a rework cycle never
resets `provider_attempt` back below what `run_provider_attempts` already
spent -- while `CHANGES_REQUIRED` at the last allowed cycle instead resolves
to `state_machine.transition`'s own `REVIEW_CYCLES_EXHAUSTED` outcome
without ever invoking one more coder. A reviewer `APPROVED`, or any
terminal failure at any step (architect, any coder cycle, any reviewer
cycle, or cycle exhaustion), is resolved into `state_machine.transition`'s
own verdict and returned to the caller: `IssuePipelineResult` itself never
marks a reviewer `APPROVED` as an early approval and never renders
CLI/`FINAL_STATUS` output -- `finalize_run` (M13-04) is the single place
that turns any such terminal verdict into one converged, best-effort
postflight/finalization outcome.

`finalize_run` converges every terminal path this module can reach --
`bootstrap_run`'s own preflight failures once a run directory exists, and
every stop `run_issue_pipeline` reports -- into one postflight/finalization
step, never invoking another agent role (System Design SS8.4, SS11.3,
SS13.3, SS16.3; M13-04). It re-checks Git exactly once more as a pure
continuity probe -- `role=None`, against the *last checkpoint the pipeline
itself accepted*, never the run's original baseline -- since no further
delta of any kind is authorized once the last role has stopped running; a
content-only change slipped in after the last accepted checkpoint is
exactly the drift this continuity check exists to catch, and comparing
against the original baseline with the coder's own tolerance would instead
mask it. The original baseline stays reserved for `evaluate_final_gate`'s
own, separate branch/HEAD-unchanged-for-the-whole-run comparison. Applies
`state_machine.resolve_terminal_outcome` and `evaluate_final_gate` -- so
`FinalStatus.APPROVED` remains possible only after a reviewer's historical
`APPROVED`, a `SAFE` postflight, an unchanged branch/HEAD since the run's
original baseline, and `OK` persistence, and a Git/logging/interrupt cause
already observed is never cancelled by a later, lower-precedence one --
quarantines
the target *before* releasing the lease whenever a child's termination
could not be confirmed (treating the postflight itself as `INDETERMINATE`
in that case, since a still-possibly-live process makes any fresh probe
unreliable), and persists exactly one final, fully-resolved `RunRecord`.
When even that last write fails, the `IssueResult` it returns falls back to
the last snapshot already known durable (System Design SS15.4) rather than
ever reporting an unpersisted `APPROVED` as genuine. The lease, whenever
one was ever acquired, is always released here, and only after this last
persist attempt -- never before, never left to a caller.

The `cli.py` composition root -- parsing argv, sequencing `bootstrap_run` /
`run_issue_pipeline` / `finalize_run`, rendering console/exit-code output
(System Design SS5.3, SS8.2; M14) -- remains out of scope here.
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
    FinalStatus,
    FrozenJsonValue,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    IssueLocator,
    IssueResult,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProviderRetryConfig,
    RetryDecision,
    ReviewStatus,
    RunOutcome,
    RunRecord,
    RunRequest,
    TargetRepository,
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
from opencode_tools.prompting import (
    build_architect_prompt,
    build_coder_prompt,
    build_reviewer_prompt,
)
from opencode_tools.retry import decide_retry
from opencode_tools.state_machine import (
    AttemptPrecedence,
    PipelineAction,
    PipelineState,
    TransitionEvent,
    TransitionEventKind,
    classify_attempt_outcome,
    evaluate_final_gate,
    resolve_exit_code,
    resolve_terminal_outcome,
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

    Unlike `record`, `last_observed_termination_confirmed` does *not* stop
    advancing once persistence is blocked: it is captured the instant an
    `AgentResult` is received, before that attempt's own `persist` call is
    even attempted, so a caller that catches the `LoggingError` a failed
    `persist` raises still learns whether the process group that just ran
    was confirmed terminated -- the one fact `finalize_run` needs to force
    postflight `INDETERMINATE` and quarantine the lease (System Design
    SS16.3; ADR-006) for a possibly still-live child, even though that same
    attempt's own `AttemptRecord` never reached `record`.

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
        self._last_agent_result: AgentResult | None = None

    @property
    def record(self) -> RunRecord:
        """The latest `RunRecord` snapshot this instance has itself durably
        persisted -- exactly the last one `run_store.persist` accepted, so a
        caller can hand it to `finalize_run` (M13-04) once the pipeline
        itself has stopped, without this class exposing any further
        internal state.
        """

        return self._record

    @property
    def last_observed_termination_confirmed(self) -> bool | None:
        """The most recently observed attempt's own `AgentResult.process.
        termination_confirmed`, captured the moment the agent returns --
        unlike `record`, this survives even when that same attempt's
        `AttemptRecord` never reaches `record` because its own `persist`
        call failed. A caller that only had `record` to fall back to would
        otherwise lose exactly the fact System Design SS16.3/ADR-006 needs
        to force postflight `INDETERMINATE` and quarantine the lease: a
        possibly still-live child process. `None` only when no agent has
        ever been invoked yet.
        """

        if self._last_agent_result is None:
            return None
        return self._last_agent_result.process.termination_confirmed

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
        self._last_agent_result = agent_result

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
    caller can keep holding it through the pipeline (System Design SS16.2)
    on success, or, on failure, exactly until `finalize_run` -- already
    called by this function whenever `issue_result` is set -- has released
    it. `orchestrator` and `issue_locator` are present if and only if
    `error` is `None`: every bootstrap/preflight step succeeded, and this
    call has not invoked -- and will never invoke -- any agent role itself
    (System Design SS8.2: "nessun agent parte su preflight non verde").
    `record` is the last `RunRecord` snapshot this call itself durably
    persisted: the fully preflighted one on success, a best-effort failure
    snapshot when a run directory already existed to update, or `None` when
    the failure happened before one could ever be created (System Design
    SS8.2 point 3: these errors "possono precedere run.json"). `issue_result`
    is `finalize_run`'s own converged outcome, always produced whenever a
    failure reaches that same "run directory already existed" point
    (M13-04) -- `record` being `None` there only means this call's own
    amend-persist never made it to disk (System Design SS15.4), not that
    `finalize_run` was skipped. It is `None` on success (the run is not
    over: postflight/finalization apply once the pipeline itself has run,
    not here) and `None` for a failure too early to ever finalize -- before
    a run directory (and the lease it follows) ever existed.
    """

    lease: TargetLease | None
    error: OpenCodeToolsError | None
    orchestrator: IssueOrchestrator | None
    issue_locator: IssueLocator | None
    record: RunRecord | None
    issue_result: IssueResult | None = None

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
            if self.issue_result is not None:
                raise ValueError("issue_result must be None when error is None")
        else:
            if not isinstance(self.error, OpenCodeToolsError):
                raise TypeError("error must be OpenCodeToolsError or None")
            if self.orchestrator is not None:
                raise ValueError("orchestrator must be None when error is set")
            if self.issue_locator is not None:
                raise ValueError("issue_locator must be None when error is set")
            if self.record is not None and type(self.record) is not RunRecord:
                raise TypeError("record must be RunRecord or None")
            if (
                self.issue_result is not None
                and type(self.issue_result) is not IssueResult
            ):
                raise TypeError("issue_result must be IssueResult or None")


def _persist_or_raise(run_store: RunStorePort, record: RunRecord) -> None:
    status = run_store.persist(record)
    if status is not PersistenceStatus.OK:
        raise LoggingError(
            "orchestrator.bootstrap_persist_failed",
            "Failed to persist run.json during bootstrap.",
            related_record=record.run_id,
        )


def _bootstrap_failure(
    *,
    lease: TargetLease | None,
    error: OpenCodeToolsError,
    record: RunRecord | None,
    issue_result: IssueResult | None = None,
) -> BootstrapOutcome:
    return BootstrapOutcome(
        lease=lease,
        error=error,
        orchestrator=None,
        issue_locator=None,
        record=record,
        issue_result=issue_result,
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

        # A run directory (and a lease) already existed: this failure is a
        # terminal path "successivo alla run init" and converges through the
        # same finalize_run every other one does (M13-04), rather than
        # leaving postflight/the lease to a caller. No agent role has ever
        # run yet, so there is nothing to confirm terminated and no reviewer
        # verdict to weigh.
        finalize_record = (
            persisted_record if persisted_record is not None else latest_record
        )
        issue_result = finalize_run(
            record=finalize_record,
            target=target,
            trigger_outcome=error.outcome,
            review_status=None,
            interrupted=False,
            termination_confirmed=None,
            git_safety=git_safety,
            run_store=run_store,
            lease=lease,
            clock=clock,
            max_review_cycles=config.execution.max_review_cycles,
        )
        return _bootstrap_failure(
            lease=lease, error=error, record=persisted_record, issue_result=issue_result
        )

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


def _attempt_result(
    result: LogicalInvocationResult,
) -> tuple[ParsedAgentResponse | None, RunOutcome | None]:
    """Combine one attempt's protocol precedence and Git safety (M13-02).

    Returns `(response, None)` iff the attempt technically succeeded
    (`precedence.outcome is SUCCEEDED`) *and* its own "after" checkpoint
    stayed `SAFE`; returns `(None, outcome)` naming the `RunOutcome` this
    pipeline should report otherwise. Protocol precedence and Git safety are
    orthogonal dimensions (System Design SS7.1) that `run_logical_invocation`
    deliberately never reconciles into one value -- this is the one place
    that combines them for the purpose of gating the *next* role invocation,
    so a mutation by architect or reviewer (or any branch/HEAD drift, for
    any role) blocks the next phase exactly as a protocol failure would,
    never silently excused because the marker itself parsed. Git safety
    wins when both are simultaneously bad, matching `resolve_terminal_outcome`
    's own `GIT_SAFETY_ERROR`-over-`trigger_outcome` precedence (SS13.3).
    """

    if result.agent_result is None:
        if result.control_plane_error is not None:
            return None, result.control_plane_error.outcome
        return None, RunOutcome.GIT_SAFETY_ERROR

    git_after = result.git_after
    if git_after is None:
        raise AssertionError("a non-blocked attempt always has a git_after checkpoint")
    if git_after.safety_status is not GitSafetyStatus.SAFE:
        return None, RunOutcome.GIT_SAFETY_ERROR

    precedence = result.precedence
    if precedence is None:
        raise AssertionError("a non-blocked attempt always has a precedence")
    if precedence.outcome is not RunOutcome.SUCCEEDED:
        return None, precedence.outcome

    response = result.agent_result.terminal_response
    if response is None:
        raise AssertionError("a SUCCEEDED precedence always has a terminal_response")
    return response, None


def _architect_event(result: LogicalInvocationResult) -> TransitionEvent:
    response, failure = _attempt_result(result)
    if (
        response is not None
        and response.agent_status is AgentStatus.READY
        and response.issue_ref is not None
    ):
        return TransitionEvent(kind=TransitionEventKind.ARCHITECT_READY)
    return TransitionEvent(
        kind=TransitionEventKind.TERMINAL_OUTCOME,
        outcome=failure if failure is not None else RunOutcome.PROTOCOL_ERROR,
    )


def _coder_event(result: LogicalInvocationResult) -> TransitionEvent:
    response, failure = _attempt_result(result)
    if response is not None and response.agent_status is AgentStatus.COMPLETED:
        return TransitionEvent(kind=TransitionEventKind.CODER_COMPLETED)
    return TransitionEvent(
        kind=TransitionEventKind.TERMINAL_OUTCOME,
        outcome=failure if failure is not None else RunOutcome.PROTOCOL_ERROR,
    )


def _reviewer_event(result: LogicalInvocationResult) -> TransitionEvent:
    response, failure = _attempt_result(result)
    if response is not None:
        if response.review_status is ReviewStatus.APPROVED:
            return TransitionEvent(kind=TransitionEventKind.REVIEWER_APPROVED)
        if response.review_status is ReviewStatus.CHANGES_REQUIRED:
            return TransitionEvent(kind=TransitionEventKind.REVIEWER_CHANGES_REQUIRED)
    return TransitionEvent(
        kind=TransitionEventKind.TERMINAL_OUTCOME,
        outcome=failure if failure is not None else RunOutcome.PROTOCOL_ERROR,
    )


def _require_invocation_cycle(
    value: object, field_name: str
) -> tuple[LogicalInvocationResult, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for item in value:
        if type(item) is not LogicalInvocationResult:
            raise TypeError(f"{field_name} must contain only LogicalInvocationResult")
    return value


def _require_invocation_cycles(
    value: object, field_name: str
) -> tuple[tuple[LogicalInvocationResult, ...], ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    cycles: list[tuple[LogicalInvocationResult, ...]] = []
    for item in value:
        cycle = _require_invocation_cycle(item, f"{field_name}[]")
        if not cycle:
            raise ValueError(f"{field_name}[] must not be empty")
        cycles.append(cycle)
    return tuple(cycles)


@dataclass(frozen=True, slots=True)
class IssuePipelineResult:
    """The typed result of composing the full nominal-through-rework path
    (System Design SS8.1, SS9, SS12.3; M13-02, M13-03).

    `architect` is every provider attempt `IssueOrchestrator.
    run_provider_attempts` actually ran for the architect's own logical
    invocation, in order; the empty tuple never occurs here (an architect
    invocation always runs). `coder_cycles`/`reviewer_cycles` are, in review
    cycle order, the same per-invocation attempt tuple for each coder/
    reviewer logical invocation this pipeline actually ran -- so
    `coder_cycles[i]`/`reviewer_cycles[i]` is review cycle `i + 1`.
    `coder_cycles` is empty exactly when the architect never established
    this pipeline's own precondition for the coder (a `READY` response
    carrying a locator-consistent `IssueRef`, with a Git-safe `SAFE`
    checkpoint throughout) -- reconstructed independently of whatever
    concrete `AgentRunner` adapter produced the attempt, never assumed from
    `AgentResult.outcome` alone (`_attempt_result`). `reviewer_cycles` has
    either exactly as many entries as `coder_cycles`, or exactly one fewer:
    the latter iff the *last* coder cycle never established the
    precondition for its own reviewer (a `COMPLETED`, Git-safe response) --
    every earlier coder cycle, by construction, was already followed by its
    own reviewer cycle, since only a reviewer `CHANGES_REQUIRED` below
    `max_review_cycles` ever opens a further coder cycle at all.

    `state`/`action`/`outcome` are exactly `state_machine.transition`'s own
    verdict for the last role actually invoked. Since this pipeline now acts
    on the rework loop itself (M13-03) rather than only reporting it,
    `action` is always `PipelineAction.ENTER_POSTFLIGHT` here: on any
    failure (architect, any coder cycle, or any reviewer cycle), on a
    reviewer `APPROVED`, or on `state_machine.TransitionEventKind.
    REVIEWER_CHANGES_REQUIRED` at the last allowed review cycle (`outcome`
    is then `RunOutcome.REVIEW_CYCLES_EXHAUSTED`, and no further coder is
    ever invoked). Postflight, finalization, and any `FinalStatus`/exit-code
    decision remain entirely out of scope: this pipeline never marks a
    reviewer `APPROVED` as an early approval and never emits
    CLI/`FINAL_STATUS` output.
    """

    architect: tuple[LogicalInvocationResult, ...]
    coder_cycles: tuple[tuple[LogicalInvocationResult, ...], ...]
    reviewer_cycles: tuple[tuple[LogicalInvocationResult, ...], ...]
    state: PipelineState
    action: PipelineAction
    outcome: RunOutcome | None

    def __post_init__(self) -> None:
        architect = _require_invocation_cycle(self.architect, "architect")
        if not architect:
            raise ValueError("architect must not be empty")
        object.__setattr__(self, "architect", architect)
        coder_cycles = _require_invocation_cycles(self.coder_cycles, "coder_cycles")
        object.__setattr__(self, "coder_cycles", coder_cycles)
        reviewer_cycles = _require_invocation_cycles(
            self.reviewer_cycles, "reviewer_cycles"
        )
        object.__setattr__(self, "reviewer_cycles", reviewer_cycles)
        if len(reviewer_cycles) > len(coder_cycles):
            raise ValueError("reviewer_cycles must not exceed coder_cycles")
        if len(coder_cycles) - len(reviewer_cycles) > 1:
            raise ValueError("only the last coder cycle may lack a reviewer cycle")
        if type(self.state) is not PipelineState:
            raise TypeError("state must be PipelineState")
        _require_exact_enum(self.action, PipelineAction, "action")
        if self.outcome is not None and type(self.outcome) is not RunOutcome:
            raise TypeError("outcome must be RunOutcome or None")


def run_issue_pipeline(
    *,
    orchestrator: IssueOrchestrator,
    issue_locator: IssueLocator,
    workspace: Workspace,
    target: TargetRepository,
    max_review_cycles: int,
) -> IssuePipelineResult:
    """Compose the full nominal-through-rework path (M13-02, M13-03).

    Invokes architect, then -- only if it reports `READY` with a locator-
    consistent `IssueRef` and stayed Git-safe -- coder at review cycle 1,
    then -- only if it reports `COMPLETED` and stayed Git-safe -- reviewer at
    review cycle 1, each as its own primary-agent session via
    `IssueOrchestrator.run_provider_attempts` (never a raw `AgentRunner.run`
    call, never a fourth agent, and never any new provider-retry logic --
    every attempt within one logical invocation is still M12's unchanged
    `run_provider_attempts` loop, so a provider retry never advances
    `review_cycle` and a rework cycle never re-runs an already-spent
    `provider_attempt`). Every opaque payload -- the architect's handoff body
    and discovered `IssueRef`, the coder's report, its final Git change
    inventory (`staged`/`unstaged`/`untracked` paths, never a diff), and --
    from review cycle 2 onward -- the reviewer's own `CHANGES_REQUIRED` body
    -- is carried into the next role's prompt via `prompting.py` verbatim;
    Python never reads, summarizes, or judges any of it (ADR-002, ADR-010).
    `test_scope` is always empty: nothing in this milestone runs or
    summarizes tests.

    A reviewer `CHANGES_REQUIRED` below `max_review_cycles` opens the next
    coder(n+1)/reviewer(n+1) cycle in the same loop, using the reviewer's own
    body as `previous_review_feedback`; at the last allowed cycle it instead
    resolves to `state_machine.transition`'s own `REVIEW_CYCLES_EXHAUSTED`
    outcome without ever invoking one more coder. Stops and returns as soon
    as a role's attempt does not clear the next role's precondition, on a
    reviewer `APPROVED`, or on cycle exhaustion -- in every case reporting
    `state_machine.transition`'s own verdict rather than deciding a final
    status here (M13-04).
    """

    if not isinstance(orchestrator, IssueOrchestrator):
        raise TypeError("orchestrator must be IssueOrchestrator")
    if type(issue_locator) is not IssueLocator:
        raise TypeError("issue_locator must be IssueLocator")
    if not isinstance(workspace, Workspace):
        raise TypeError("workspace must be Workspace")
    if not isinstance(target, TargetRepository):
        raise TypeError("target must be TargetRepository")
    _require_int(max_review_cycles, "max_review_cycles", minimum=1)

    architect_prompt = build_architect_prompt(
        issue_locator=issue_locator,
        workspace_root=workspace.root,
        target_root=target.root,
    )
    architect_results = orchestrator.run_provider_attempts(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        prompt=architect_prompt,
        workspace=workspace,
    )
    architect_final = architect_results[-1]
    architect_transition = transition(
        PipelineState(phase=PipelinePhase.ARCHITECT),
        _architect_event(architect_final),
        max_review_cycles=max_review_cycles,
    )
    if architect_transition.action is not PipelineAction.INVOKE_CODER:
        return IssuePipelineResult(
            architect=architect_results,
            coder_cycles=(),
            reviewer_cycles=(),
            state=architect_transition.state,
            action=architect_transition.action,
            outcome=architect_transition.outcome,
        )

    architect_response, _ = _attempt_result(architect_final)
    if architect_response is None or architect_response.issue_ref is None:
        raise AssertionError("INVOKE_CODER implies a READY response with an issue_ref")
    issue_ref = architect_response.issue_ref
    architect_handoff = architect_response.body

    coder_cycles: list[tuple[LogicalInvocationResult, ...]] = []
    reviewer_cycles: list[tuple[LogicalInvocationResult, ...]] = []
    review_cycle = 1
    previous_review_feedback: str | None = None

    while True:
        coder_prompt = build_coder_prompt(
            issue_ref=issue_ref,
            architect_handoff=architect_handoff,
            target_root=target.root,
            review_cycle=review_cycle,
            max_review_cycles=max_review_cycles,
            previous_review_feedback=previous_review_feedback,
        )
        coder_results = orchestrator.run_provider_attempts(
            role=AgentRole.CODER,
            review_cycle=review_cycle,
            prompt=coder_prompt,
            workspace=workspace,
        )
        coder_cycles.append(coder_results)
        coder_final = coder_results[-1]
        coder_transition = transition(
            PipelineState(phase=PipelinePhase.CODER, review_cycle=review_cycle),
            _coder_event(coder_final),
            max_review_cycles=max_review_cycles,
        )
        if coder_transition.action is not PipelineAction.INVOKE_REVIEWER:
            return IssuePipelineResult(
                architect=architect_results,
                coder_cycles=tuple(coder_cycles),
                reviewer_cycles=tuple(reviewer_cycles),
                state=coder_transition.state,
                action=coder_transition.action,
                outcome=coder_transition.outcome,
            )

        coder_response, _ = _attempt_result(coder_final)
        if coder_response is None:
            raise AssertionError("INVOKE_REVIEWER implies a COMPLETED coder response")
        coder_report = coder_response.body
        if coder_final.git_after is None:
            raise AssertionError(
                "a non-blocked attempt always has a git_after checkpoint"
            )
        inventory = coder_final.git_after.state

        reviewer_prompt = build_reviewer_prompt(
            issue_ref=issue_ref,
            architect_handoff=architect_handoff,
            coder_report=coder_report,
            staged=inventory.staged,
            unstaged=inventory.unstaged,
            untracked=inventory.untracked,
            test_scope="",
            target_root=target.root,
            review_cycle=review_cycle,
            max_review_cycles=max_review_cycles,
        )
        reviewer_results = orchestrator.run_provider_attempts(
            role=AgentRole.REVIEWER,
            review_cycle=review_cycle,
            prompt=reviewer_prompt,
            workspace=workspace,
        )
        reviewer_cycles.append(reviewer_results)
        reviewer_final = reviewer_results[-1]
        reviewer_transition = transition(
            PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=review_cycle),
            _reviewer_event(reviewer_final),
            max_review_cycles=max_review_cycles,
        )
        if reviewer_transition.action is not PipelineAction.INVOKE_CODER:
            return IssuePipelineResult(
                architect=architect_results,
                coder_cycles=tuple(coder_cycles),
                reviewer_cycles=tuple(reviewer_cycles),
                state=reviewer_transition.state,
                action=reviewer_transition.action,
                outcome=reviewer_transition.outcome,
            )

        # REVIEWER_CHANGES_REQUIRED below max_review_cycles: rework, using
        # the reviewer's own body as the next coder's feedback (M13-03).
        reviewer_response, _ = _attempt_result(reviewer_final)
        if reviewer_response is None:
            raise AssertionError(
                "INVOKE_CODER implies a CHANGES_REQUIRED reviewer response"
            )
        previous_review_feedback = reviewer_response.body
        next_review_cycle = reviewer_transition.state.review_cycle
        if next_review_cycle is None:
            raise AssertionError("INVOKE_CODER always carries a review_cycle")
        review_cycle = next_review_cycle


_POSTFLIGHT_CHECK_PURPOSE = "postflight"

_TERMINATION_UNCONFIRMED_QUARANTINE_REASON = (
    "orchestrator.termination_unconfirmed: a child process group's "
    "termination could not be confirmed before releasing this target's "
    "lease (System Design SS16.3; ADR-006)."
)


def _last_accepted_git_state(record: RunRecord) -> GitState | None:
    """Reconstruct the last checkpoint `IssueOrchestrator` itself accepted.

    Mirrors `IssueOrchestrator`'s own `_last_accepted_git_state` bookkeeping
    exactly (System Design SS8.3): starts at `record.git_baseline` and
    advances to an attempt's `git_after.state` only when that check was
    itself `SAFE`, in `record.attempts`' own chronological order -- an
    `AttemptRecord` only ever exists for a completed attempt (a `before`
    blocked by Git safety or control-plane drift is never persisted as one),
    so every `git_after` here is a real, completed checkpoint. An `UNSAFE`
    or `INDETERMINATE` `after` leaves the last accepted state exactly where
    it was, since that role's own drift was never accepted as a new
    checkpoint in the first place.
    """

    state = record.git_baseline
    for attempt in record.attempts:
        if attempt.git_after.safety_status is GitSafetyStatus.SAFE:
            state = attempt.git_after.state
    return state


def finalize_run(
    *,
    record: RunRecord,
    target: TargetRepository,
    trigger_outcome: RunOutcome,
    review_status: ReviewStatus | None,
    interrupted: bool,
    termination_confirmed: bool | None,
    git_safety: GitSafetyPort,
    run_store: RunStorePort,
    lease: TargetLease | None,
    clock: Clock,
    max_review_cycles: int,
) -> IssueResult:
    """Converge one terminal path into postflight and finalization (M13-04).

    Called once, whenever any terminal path -- `bootstrap_run`'s own
    preflight failures once a run directory exists, or every stop
    `run_issue_pipeline` reports -- has nothing left to do but reach
    `FINISHED`; never invokes an agent role.

    Re-checks Git exactly once more (`GitSafetyPort.check`, `role=None`, as
    a pure continuity probe) against the *last checkpoint the pipeline
    itself accepted* -- never `record.git_baseline`, the run's original
    baseline, which `evaluate_final_gate` still compares separately for the
    run's own overall branch/HEAD inventory -- whenever such a checkpoint
    exists; skipped, and treated as `GitSafetyStatus.INDETERMINATE`, when it
    does not (System Design SS11.3). Since no further delta is authorized
    once the last role has stopped running, `role=None` means even a
    content-only change is `UNSAFE` here, unlike a role's own `after` check.
    A `termination_confirmed` of `False` -- a child process
    group that might still be alive -- likewise forces this postflight
    determination to `INDETERMINATE` for gate/precedence purposes even when
    the fresh probe itself came back clean (a live survivor makes any
    contemporaneous probe unreliable); the real probe evidence, whatever it
    was, is still preserved verbatim on the persisted `git_postflight`.
    `False` also quarantines the target (`TargetLease.quarantine`) *before*
    the lease is ever released (System Design SS16.3; ADR-006) -- a
    quarantine write failure is folded into the persisted record's own
    `errors` (System Design SS15.4) rather than raised or swallowed, so it
    stays visible without masking `trigger_outcome`.

    `state_machine.resolve_terminal_outcome` and `evaluate_final_gate` are
    the only two places that decide the resolved terminal category and
    `FinalStatus`, exactly as frozen elsewhere in this module (SS8.4,
    SS13.3): `review_status` is the reviewer's own last, *historical* verdict
    (never rewritten, never overridden by a later drift -- it is the gate,
    not the record of what the reviewer said, that denies approval on
    drift), and every already-observed cause (Git safety, persistence,
    interruption) keeps its place in `resolve_terminal_outcome`'s precedence
    without cancelling `trigger_outcome`, the technical reason this path
    reached postflight in the first place.

    Persists exactly one final `RunRecord`, advancing `current_phase`
    through `POSTFLIGHT`/`FINALIZATION` to `FINISHED` via
    `state_machine.transition` like any other phase change, and marking
    `changes_preserved=True` unconditionally -- Python never mutates or
    recovers the target, on success or failure alike (FR-050). The lease,
    if there is one, is released only after this attempt, regardless of its
    outcome. If it fails, the returned `IssueResult` falls back to `record`
    -- the last snapshot already known durable -- with `FinalStatus.FAILED`
    and the freshly-resolved `LOGGING_ERROR`-inclusive terminal outcome,
    never reporting an unpersisted `APPROVED` as genuine (System Design
    SS15.4).
    """

    if type(record) is not RunRecord:
        raise TypeError("record must be RunRecord")
    if not isinstance(target, TargetRepository):
        raise TypeError("target must be TargetRepository")
    _require_exact_enum(trigger_outcome, RunOutcome, "trigger_outcome")
    if review_status is not None:
        _require_exact_enum(review_status, ReviewStatus, "review_status")
    if type(interrupted) is not bool:
        raise TypeError("interrupted must be a boolean")
    if termination_confirmed is not None and type(termination_confirmed) is not bool:
        raise TypeError("termination_confirmed must be a boolean or None")
    _require_int(max_review_cycles, "max_review_cycles", minimum=1)

    next_sequence = max((check.sequence for check in record.git_checks), default=-1) + 1
    last_accepted_state = _last_accepted_git_state(record)
    postflight: GitCheckRecord | None = None
    if last_accepted_state is not None:
        postflight = git_safety.check(
            target,
            sequence=next_sequence,
            purpose=_POSTFLIGHT_CHECK_PURPOSE,
            role=None,
            baseline=last_accepted_state,
        )

    effective_git_safety_status = (
        GitSafetyStatus.INDETERMINATE
        if postflight is None or termination_confirmed is False
        else postflight.safety_status
    )

    errors = record.errors
    if lease is not None and termination_confirmed is False:
        try:
            lease.quarantine(_TERMINATION_UNCONFIRMED_QUARANTINE_REASON)
        except LoggingError as quarantine_error:
            errors = (
                *errors,
                *to_error_records(
                    quarantine_error,
                    phase=PipelinePhase.POSTFLIGHT,
                    timestamp=clock.now(),
                    first_sequence=(errors[-1].sequence + 1 if errors else 0),
                ),
            )

    terminal_precedence = resolve_terminal_outcome(
        trigger_outcome=trigger_outcome,
        git_safety_status=effective_git_safety_status,
        interrupted=interrupted,
        persistence_status=record.persistence_status,
    )
    final_status = evaluate_final_gate(
        review_status=review_status,
        postflight_git_safety_status=effective_git_safety_status,
        baseline_branch=record.git_baseline.branch if record.git_baseline else None,
        baseline_head=record.git_baseline.head if record.git_baseline else None,
        postflight_branch=postflight.state.branch if postflight is not None else None,
        postflight_head=postflight.state.head if postflight is not None else None,
        persistence_status=record.persistence_status,
    )
    expected_exit_code = resolve_exit_code(
        final_status=final_status,
        terminal_outcome=terminal_precedence.terminal_outcome,
    )

    postflight_transition = transition(
        PipelineState(phase=PipelinePhase.POSTFLIGHT),
        TransitionEvent(kind=TransitionEventKind.POSTFLIGHT_COMPLETED),
        max_review_cycles=max_review_cycles,
    )
    finalization_transition = transition(
        postflight_transition.state,
        TransitionEvent(kind=TransitionEventKind.FINALIZATION_COMPLETED),
        max_review_cycles=max_review_cycles,
    )

    finished_at = clock.now()
    duration_ns = max(
        int((finished_at - record.started_at).total_seconds() * 1_000_000_000), 0
    )

    final_record = replace(
        record,
        current_phase=finalization_transition.state.phase,
        finished_at=finished_at,
        duration_ns=duration_ns,
        terminal_outcome=terminal_precedence.terminal_outcome,
        git_postflight=postflight,
        git_safety_status=effective_git_safety_status,
        errors=errors,
        trigger_outcome=terminal_precedence.terminal_outcome,
        final_status=final_status,
        expected_exit_code=expected_exit_code,
        changes_preserved=True,
        termination_confirmed=termination_confirmed,
    )

    persist_status = run_store.persist(final_record)

    if lease is not None:
        lease.__exit__(None, None, None)

    if persist_status is PersistenceStatus.OK:
        return IssueResult(
            run_id=final_record.run_id,
            artifact_path=final_record.artifact_path,
            final_status=final_status,
            expected_exit_code=expected_exit_code,
            trigger_outcome=terminal_precedence.terminal_outcome,
            git_safety_status=effective_git_safety_status,
            persistence_status=PersistenceStatus.OK,
            changes_preserved=True,
            termination_confirmed=termination_confirmed,
        )

    # The final write itself failed: `record` -- already durable when this
    # function was called -- remains the last valid run.json (System Design
    # SS15.4); an unpersisted APPROVED is never reported as genuine.
    fallback_precedence = resolve_terminal_outcome(
        trigger_outcome=trigger_outcome,
        git_safety_status=effective_git_safety_status,
        interrupted=interrupted,
        persistence_status=persist_status,
    )
    return IssueResult(
        run_id=record.run_id,
        artifact_path=record.artifact_path,
        final_status=FinalStatus.FAILED,
        expected_exit_code=resolve_exit_code(
            final_status=FinalStatus.FAILED,
            terminal_outcome=fallback_precedence.terminal_outcome,
        ),
        trigger_outcome=fallback_precedence.terminal_outcome,
        git_safety_status=effective_git_safety_status,
        persistence_status=persist_status,
        changes_preserved=True,
        termination_confirmed=termination_confirmed,
    )


__all__ = (
    "BootstrapOutcome",
    "InvocationEvent",
    "InvocationEventKind",
    "IssueOrchestrator",
    "IssuePipelineResult",
    "LogicalInvocationResult",
    "bootstrap_run",
    "finalize_run",
    "run_issue_pipeline",
)

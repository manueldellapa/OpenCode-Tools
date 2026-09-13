"""Pure phase transitions, precedence, and the final gate for the pipeline.

This module owns the lifecycle graph
`PREFLIGHT -> ARCHITECT -> CODER(n) -> REVIEWER(n) -> POSTFLIGHT ->
FINALIZATION -> FINISHED` (System Design SS8.1) and its review-cycle
counter; the precedence rules for one attempt and for the whole pipeline
(SS13.1-SS13.3); and the final approval gate plus the outcome/exit-code
mapping (SS8.4, SS13.4). It reasons exclusively over already classified
typed facts; it performs no process execution, no Git inspection, no
sleeping, and no raw agent-text interpretation, and it does not decide
provider retries (`retry.py`) or collapse concurrent causes into a single
enum.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from opencode_tools.domain import (
    FinalStatus,
    GitSafetyStatus,
    PersistenceStatus,
    PipelinePhase,
    ReviewStatus,
    RunOutcome,
)

_CYCLE_PHASES = frozenset({PipelinePhase.CODER, PipelinePhase.REVIEWER})
_FAILABLE_PHASES = frozenset(
    {
        PipelinePhase.PREFLIGHT,
        PipelinePhase.ARCHITECT,
        PipelinePhase.CODER,
        PipelinePhase.REVIEWER,
    }
)


class TransitionEventKind(StrEnum):
    """The typed signals `transition` accepts, one per lifecycle arrow."""

    PREFLIGHT_SUCCEEDED = "PREFLIGHT_SUCCEEDED"
    ARCHITECT_READY = "ARCHITECT_READY"
    CODER_COMPLETED = "CODER_COMPLETED"
    REVIEWER_APPROVED = "REVIEWER_APPROVED"
    REVIEWER_CHANGES_REQUIRED = "REVIEWER_CHANGES_REQUIRED"
    TERMINAL_OUTCOME = "TERMINAL_OUTCOME"
    POSTFLIGHT_COMPLETED = "POSTFLIGHT_COMPLETED"
    FINALIZATION_COMPLETED = "FINALIZATION_COMPLETED"


class PipelineAction(StrEnum):
    """The single next action the caller must take after a transition."""

    INVOKE_ARCHITECT = "INVOKE_ARCHITECT"
    INVOKE_CODER = "INVOKE_CODER"
    INVOKE_REVIEWER = "INVOKE_REVIEWER"
    ENTER_POSTFLIGHT = "ENTER_POSTFLIGHT"
    ENTER_FINALIZATION = "ENTER_FINALIZATION"
    FINISH = "FINISH"


def _require_exact_enum(
    value: object, enum_type: type[StrEnum], field_name: str
) -> None:
    if type(value) is not enum_type:
        raise TypeError(f"{field_name} must be {enum_type.__name__}")


@dataclass(frozen=True, slots=True)
class PipelineState:
    """The minimal lifecycle position the state machine reasons over.

    `review_cycle` is the reviewer decision count for the current or most
    recent coder/reviewer pair: required from 1 while `phase` is `CODER` or
    `REVIEWER`, and absent everywhere else.
    """

    phase: PipelinePhase
    review_cycle: int | None = None

    def __post_init__(self) -> None:
        _require_exact_enum(self.phase, PipelinePhase, "phase")
        if self.review_cycle is not None:
            if type(self.review_cycle) is not int:
                raise TypeError("review_cycle must be an integer or None")
            if self.review_cycle < 1:
                raise ValueError("review_cycle must be at least 1")
        if self.phase in _CYCLE_PHASES:
            if self.review_cycle is None:
                raise ValueError(f"{self.phase} requires a review_cycle")
        elif self.review_cycle is not None:
            raise ValueError(f"{self.phase} must not carry a review_cycle")


@dataclass(frozen=True, slots=True)
class TransitionEvent:
    """One typed signal offered to `transition`.

    `outcome` is required if and only if `kind` is `TERMINAL_OUTCOME`; it
    names the already classified `RunOutcome` that forced the pipeline out
    of its current phase.
    """

    kind: TransitionEventKind
    outcome: RunOutcome | None = None

    def __post_init__(self) -> None:
        _require_exact_enum(self.kind, TransitionEventKind, "kind")
        if self.kind is TransitionEventKind.TERMINAL_OUTCOME:
            _require_exact_enum(self.outcome, RunOutcome, "outcome")
            if self.outcome is RunOutcome.SUCCEEDED:
                raise ValueError("TERMINAL_OUTCOME must not be SUCCEEDED")
        elif self.outcome is not None:
            raise ValueError(f"{self.kind} must not carry an outcome")


@dataclass(frozen=True, slots=True)
class Transition:
    """The pure result of applying one event: the next state and its action.

    `outcome` is set when the transition was forced by a terminal outcome or
    by review-cycle exhaustion, and is `None` for every ordinary advance.
    """

    state: PipelineState
    action: PipelineAction
    outcome: RunOutcome | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not PipelineState:
            raise TypeError("state must be PipelineState")
        _require_exact_enum(self.action, PipelineAction, "action")
        if self.outcome is not None:
            _require_exact_enum(self.outcome, RunOutcome, "outcome")


def transition(
    state: PipelineState,
    event: TransitionEvent,
    *,
    max_review_cycles: int,
) -> Transition:
    """Apply `event` to `state` and return the resulting state and action.

    This function is total over its typed inputs: every `(phase, event.kind)`
    pair outside the canonical `PREFLIGHT -> ... -> FINISHED` graph raises
    `ValueError`. The review cycle starts at 1 on the first coder invocation,
    increments only after a non-exhausting `CHANGES_REQUIRED`, and never
    triggers another coder once `max_review_cycles` is reached.
    """

    if type(state) is not PipelineState:
        raise TypeError("state must be PipelineState")
    if type(event) is not TransitionEvent:
        raise TypeError("event must be TransitionEvent")
    if type(max_review_cycles) is not int:
        raise TypeError("max_review_cycles must be an integer")
    if max_review_cycles < 1:
        raise ValueError("max_review_cycles must be at least 1")

    phase, kind = state.phase, event.kind

    match (phase, kind):
        case (PipelinePhase.PREFLIGHT, TransitionEventKind.PREFLIGHT_SUCCEEDED):
            return Transition(
                state=PipelineState(phase=PipelinePhase.ARCHITECT),
                action=PipelineAction.INVOKE_ARCHITECT,
            )
        case (PipelinePhase.ARCHITECT, TransitionEventKind.ARCHITECT_READY):
            return Transition(
                state=PipelineState(phase=PipelinePhase.CODER, review_cycle=1),
                action=PipelineAction.INVOKE_CODER,
            )
        case (PipelinePhase.CODER, TransitionEventKind.CODER_COMPLETED):
            cycle = cast(int, state.review_cycle)
            return Transition(
                state=PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=cycle),
                action=PipelineAction.INVOKE_REVIEWER,
            )
        case (PipelinePhase.REVIEWER, TransitionEventKind.REVIEWER_APPROVED):
            return Transition(
                state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
                action=PipelineAction.ENTER_POSTFLIGHT,
            )
        case (PipelinePhase.REVIEWER, TransitionEventKind.REVIEWER_CHANGES_REQUIRED):
            cycle = cast(int, state.review_cycle)
            if cycle < max_review_cycles:
                return Transition(
                    state=PipelineState(
                        phase=PipelinePhase.CODER, review_cycle=cycle + 1
                    ),
                    action=PipelineAction.INVOKE_CODER,
                )
            return Transition(
                state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
                action=PipelineAction.ENTER_POSTFLIGHT,
                outcome=RunOutcome.REVIEW_CYCLES_EXHAUSTED,
            )
        case (PipelinePhase.POSTFLIGHT, TransitionEventKind.POSTFLIGHT_COMPLETED):
            return Transition(
                state=PipelineState(phase=PipelinePhase.FINALIZATION),
                action=PipelineAction.ENTER_FINALIZATION,
            )
        case (PipelinePhase.FINALIZATION, TransitionEventKind.FINALIZATION_COMPLETED):
            return Transition(
                state=PipelineState(phase=PipelinePhase.FINISHED),
                action=PipelineAction.FINISH,
            )
        case (_, TransitionEventKind.TERMINAL_OUTCOME) if phase in _FAILABLE_PHASES:
            return Transition(
                state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
                action=PipelineAction.ENTER_POSTFLIGHT,
                outcome=event.outcome,
            )
        case _:
            raise ValueError(f"{kind} is not a valid transition from {phase}")


_ATTEMPT_SIGNAL_PRECEDENCE: tuple[RunOutcome, ...] = (
    RunOutcome.TIMEOUT,
    RunOutcome.PROVIDER_ERROR,
    RunOutcome.PROCESS_ERROR,
    RunOutcome.PROTOCOL_ERROR,
    RunOutcome.AGENT_REPORTED_FAILURE,
    RunOutcome.SUCCEEDED,
)


@dataclass(frozen=True, slots=True)
class AttemptPrecedence:
    """The precedence-resolved outcome for one attempt (System Design SS13.2).

    `concurrent_signals` preserves every true signal, highest precedence
    first, so a lower-priority cause is never silently discarded even
    though it never overrides `outcome`.
    """

    outcome: RunOutcome
    concurrent_signals: tuple[RunOutcome, ...]

    def __post_init__(self) -> None:
        _require_exact_enum(self.outcome, RunOutcome, "outcome")
        if type(self.concurrent_signals) is not tuple:
            raise TypeError("concurrent_signals must be a tuple")
        if not self.concurrent_signals:
            raise ValueError("concurrent_signals must not be empty")
        if self.concurrent_signals[0] is not self.outcome:
            raise ValueError("outcome must be the highest-precedence signal")


def classify_attempt_outcome(
    *,
    timed_out: bool,
    provider_error: bool,
    process_error: bool,
    protocol_error: bool,
    agent_reported_failure: bool,
    succeeded: bool,
) -> AttemptPrecedence:
    """Resolve one attempt's already-classified technical signals (SS13.2).

    Precedence is `TIMEOUT > PROVIDER_ERROR > PROCESS_ERROR > PROTOCOL_ERROR
    > AGENT_REPORTED_FAILURE > SUCCEEDED`; Git safety is a separate,
    orthogonal dimension and never participates here. At least one signal
    must be true.
    """

    signal_by_outcome = {
        RunOutcome.TIMEOUT: timed_out,
        RunOutcome.PROVIDER_ERROR: provider_error,
        RunOutcome.PROCESS_ERROR: process_error,
        RunOutcome.PROTOCOL_ERROR: protocol_error,
        RunOutcome.AGENT_REPORTED_FAILURE: agent_reported_failure,
        RunOutcome.SUCCEEDED: succeeded,
    }
    for outcome, value in signal_by_outcome.items():
        if type(value) is not bool:
            raise TypeError(f"{outcome.name.lower()} must be a boolean")

    present = tuple(
        outcome for outcome in _ATTEMPT_SIGNAL_PRECEDENCE if signal_by_outcome[outcome]
    )
    if not present:
        raise ValueError("at least one attempt signal must be true")
    return AttemptPrecedence(outcome=present[0], concurrent_signals=present)


@dataclass(frozen=True, slots=True)
class TerminalPrecedence:
    """The precedence-resolved terminal outcome for a run (System Design SS13.3).

    `concurrent_causes` preserves every contributing cause in chronological
    order (the original trigger first), so this precedence never erases an
    earlier cause even though it never overrides `terminal_outcome`.
    """

    terminal_outcome: RunOutcome
    concurrent_causes: tuple[RunOutcome, ...]

    def __post_init__(self) -> None:
        _require_exact_enum(self.terminal_outcome, RunOutcome, "terminal_outcome")
        if type(self.concurrent_causes) is not tuple:
            raise TypeError("concurrent_causes must be a tuple")
        if not self.concurrent_causes:
            raise ValueError("concurrent_causes must not be empty")


def resolve_terminal_outcome(
    *,
    trigger_outcome: RunOutcome,
    git_safety_status: GitSafetyStatus,
    interrupted: bool,
    persistence_status: PersistenceStatus,
) -> TerminalPrecedence:
    """Resolve the pipeline terminal outcome (System Design SS13.3).

    Precedence is `LOGGING_ERROR > GIT_SAFETY_ERROR/INDETERMINATE >
    INTERRUPTED > trigger_outcome`; this never cancels an earlier cause, it
    only picks which one is reported as `terminal_outcome`.
    """

    _require_exact_enum(trigger_outcome, RunOutcome, "trigger_outcome")
    _require_exact_enum(git_safety_status, GitSafetyStatus, "git_safety_status")
    if type(interrupted) is not bool:
        raise TypeError("interrupted must be a boolean")
    _require_exact_enum(persistence_status, PersistenceStatus, "persistence_status")

    causes: list[RunOutcome] = [trigger_outcome]

    def _add_cause(cause: RunOutcome) -> None:
        if cause not in causes:
            causes.append(cause)

    if interrupted:
        _add_cause(RunOutcome.INTERRUPTED)
    if git_safety_status is not GitSafetyStatus.SAFE:
        _add_cause(RunOutcome.GIT_SAFETY_ERROR)
    if persistence_status is not PersistenceStatus.OK:
        _add_cause(RunOutcome.LOGGING_ERROR)

    if RunOutcome.LOGGING_ERROR in causes:
        winner = RunOutcome.LOGGING_ERROR
    elif RunOutcome.GIT_SAFETY_ERROR in causes:
        winner = RunOutcome.GIT_SAFETY_ERROR
    elif RunOutcome.INTERRUPTED in causes:
        winner = RunOutcome.INTERRUPTED
    else:
        winner = trigger_outcome

    return TerminalPrecedence(terminal_outcome=winner, concurrent_causes=tuple(causes))


def evaluate_final_gate(
    *,
    review_status: ReviewStatus | None,
    postflight_git_safety_status: GitSafetyStatus,
    baseline_branch: str | None,
    baseline_head: str | None,
    postflight_branch: str | None,
    postflight_head: str | None,
    persistence_status: PersistenceStatus,
) -> FinalStatus:
    """Decide the single final status (System Design SS8.4).

    `APPROVED` requires every one of: a reviewer `APPROVED`, a `SAFE`
    postflight, an unchanged branch and HEAD versus the baseline, and
    `persistence_status == OK`. This is fail-closed: a missing baseline or
    postflight fact denies approval rather than being treated as a match,
    and a reviewer `APPROVED` undone by later Git drift or a persistence
    failure still produces `FAILED`.
    """

    if review_status is not None:
        _require_exact_enum(review_status, ReviewStatus, "review_status")
    _require_exact_enum(
        postflight_git_safety_status,
        GitSafetyStatus,
        "postflight_git_safety_status",
    )
    for value, field_name in (
        (baseline_branch, "baseline_branch"),
        (baseline_head, "baseline_head"),
        (postflight_branch, "postflight_branch"),
        (postflight_head, "postflight_head"),
    ):
        if value is not None and type(value) is not str:
            raise TypeError(f"{field_name} must be a string or None")
    _require_exact_enum(persistence_status, PersistenceStatus, "persistence_status")

    approved = (
        review_status is ReviewStatus.APPROVED
        and postflight_git_safety_status is GitSafetyStatus.SAFE
        and baseline_branch is not None
        and postflight_branch == baseline_branch
        and baseline_head is not None
        and postflight_head == baseline_head
        and persistence_status is PersistenceStatus.OK
    )
    return FinalStatus.APPROVED if approved else FinalStatus.FAILED


_EXIT_CODE_BY_OUTCOME: dict[RunOutcome, int] = {
    RunOutcome.SUCCEEDED: 20,
    RunOutcome.CONFIG_ERROR: 10,
    RunOutcome.PREFLIGHT_ERROR: 10,
    RunOutcome.PROVIDER_ERROR: 20,
    RunOutcome.PROCESS_ERROR: 20,
    RunOutcome.TIMEOUT: 20,
    RunOutcome.PROTOCOL_ERROR: 20,
    RunOutcome.AGENT_REPORTED_FAILURE: 20,
    RunOutcome.REVIEW_CYCLES_EXHAUSTED: 20,
    RunOutcome.GIT_SAFETY_ERROR: 30,
    RunOutcome.LOGGING_ERROR: 40,
    RunOutcome.INTERRUPTED: 20,
}


def resolve_exit_code(
    *, final_status: FinalStatus, terminal_outcome: RunOutcome
) -> int:
    """Map a final decision to its exit-code family (System Design SS13.4).

    Exit code 0 depends only on `final_status`. Exit codes 2 (`argparse`
    usage) and 130 (a SIGINT before any run could be initialized) are
    `cli.py`'s responsibility: they precede any `RunRecord` and this
    total mapping is never consulted for them.
    """

    _require_exact_enum(final_status, FinalStatus, "final_status")
    _require_exact_enum(terminal_outcome, RunOutcome, "terminal_outcome")

    if final_status is FinalStatus.APPROVED:
        return 0
    return _EXIT_CODE_BY_OUTCOME[terminal_outcome]


__all__ = (
    "AttemptPrecedence",
    "PipelineAction",
    "PipelineState",
    "TerminalPrecedence",
    "Transition",
    "TransitionEvent",
    "TransitionEventKind",
    "classify_attempt_outcome",
    "evaluate_final_gate",
    "resolve_exit_code",
    "resolve_terminal_outcome",
    "transition",
)

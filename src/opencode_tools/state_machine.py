"""Pure phase transitions and review-cycle bookkeeping for the pipeline.

This module owns only the lifecycle graph
`PREFLIGHT -> ARCHITECT -> CODER(n) -> REVIEWER(n) -> POSTFLIGHT ->
FINALIZATION -> FINISHED` (System Design SS8.1) and the review-cycle counter
that drives the coder/reviewer loop. It reasons exclusively over already
classified typed events; it performs no process execution, no Git
inspection, no sleeping, and no raw agent-text interpretation, and it does
not decide provider retries, precedence between concurrent causes, or the
final status gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from opencode_tools.domain import PipelinePhase, RunOutcome

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


__all__ = (
    "PipelineAction",
    "PipelineState",
    "Transition",
    "TransitionEvent",
    "TransitionEventKind",
    "transition",
)

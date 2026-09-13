"""Unit tests for the pure lifecycle transitions and review-cycle counter."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from enum import StrEnum
from typing import cast

import pytest

from opencode_tools.domain import PipelinePhase, RunOutcome
from opencode_tools.state_machine import (
    PipelineAction,
    PipelineState,
    Transition,
    TransitionEvent,
    TransitionEventKind,
    transition,
)

_CYCLE_PHASES = frozenset({PipelinePhase.CODER, PipelinePhase.REVIEWER})


def _state_for(phase: PipelinePhase, *, review_cycle: int = 1) -> PipelineState:
    if phase in _CYCLE_PHASES:
        return PipelineState(phase=phase, review_cycle=review_cycle)
    return PipelineState(phase=phase)


VALID_TRANSITIONS: tuple[
    tuple[str, PipelineState, TransitionEvent, int, Transition], ...
] = (
    (
        "preflight_succeeded",
        PipelineState(phase=PipelinePhase.PREFLIGHT),
        TransitionEvent(kind=TransitionEventKind.PREFLIGHT_SUCCEEDED),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.ARCHITECT),
            action=PipelineAction.INVOKE_ARCHITECT,
        ),
    ),
    (
        "architect_ready",
        PipelineState(phase=PipelinePhase.ARCHITECT),
        TransitionEvent(kind=TransitionEventKind.ARCHITECT_READY),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.CODER, review_cycle=1),
            action=PipelineAction.INVOKE_CODER,
        ),
    ),
    (
        "coder_completed_cycle_1",
        PipelineState(phase=PipelinePhase.CODER, review_cycle=1),
        TransitionEvent(kind=TransitionEventKind.CODER_COMPLETED),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=1),
            action=PipelineAction.INVOKE_REVIEWER,
        ),
    ),
    (
        "coder_completed_cycle_2_preserves_cycle",
        PipelineState(phase=PipelinePhase.CODER, review_cycle=2),
        TransitionEvent(kind=TransitionEventKind.CODER_COMPLETED),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=2),
            action=PipelineAction.INVOKE_REVIEWER,
        ),
    ),
    (
        "reviewer_approved",
        PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=1),
        TransitionEvent(kind=TransitionEventKind.REVIEWER_APPROVED),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
            action=PipelineAction.ENTER_POSTFLIGHT,
        ),
    ),
    (
        "reviewer_changes_required_below_limit_advances_cycle",
        PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=1),
        TransitionEvent(kind=TransitionEventKind.REVIEWER_CHANGES_REQUIRED),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.CODER, review_cycle=2),
            action=PipelineAction.INVOKE_CODER,
        ),
    ),
    (
        "reviewer_changes_required_at_limit_exhausts_without_a_coder",
        PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=3),
        TransitionEvent(kind=TransitionEventKind.REVIEWER_CHANGES_REQUIRED),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
            action=PipelineAction.ENTER_POSTFLIGHT,
            outcome=RunOutcome.REVIEW_CYCLES_EXHAUSTED,
        ),
    ),
    (
        "reviewer_changes_required_exhausts_immediately_when_limit_is_one",
        PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=1),
        TransitionEvent(kind=TransitionEventKind.REVIEWER_CHANGES_REQUIRED),
        1,
        Transition(
            state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
            action=PipelineAction.ENTER_POSTFLIGHT,
            outcome=RunOutcome.REVIEW_CYCLES_EXHAUSTED,
        ),
    ),
    (
        "postflight_completed",
        PipelineState(phase=PipelinePhase.POSTFLIGHT),
        TransitionEvent(kind=TransitionEventKind.POSTFLIGHT_COMPLETED),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.FINALIZATION),
            action=PipelineAction.ENTER_FINALIZATION,
        ),
    ),
    (
        "finalization_completed",
        PipelineState(phase=PipelinePhase.FINALIZATION),
        TransitionEvent(kind=TransitionEventKind.FINALIZATION_COMPLETED),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.FINISHED),
            action=PipelineAction.FINISH,
        ),
    ),
    (
        "terminal_outcome_from_preflight",
        PipelineState(phase=PipelinePhase.PREFLIGHT),
        TransitionEvent(
            kind=TransitionEventKind.TERMINAL_OUTCOME,
            outcome=RunOutcome.PREFLIGHT_ERROR,
        ),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
            action=PipelineAction.ENTER_POSTFLIGHT,
            outcome=RunOutcome.PREFLIGHT_ERROR,
        ),
    ),
    (
        "terminal_outcome_from_architect",
        PipelineState(phase=PipelinePhase.ARCHITECT),
        TransitionEvent(
            kind=TransitionEventKind.TERMINAL_OUTCOME,
            outcome=RunOutcome.AGENT_REPORTED_FAILURE,
        ),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
            action=PipelineAction.ENTER_POSTFLIGHT,
            outcome=RunOutcome.AGENT_REPORTED_FAILURE,
        ),
    ),
    (
        "terminal_outcome_from_coder",
        PipelineState(phase=PipelinePhase.CODER, review_cycle=2),
        TransitionEvent(
            kind=TransitionEventKind.TERMINAL_OUTCOME, outcome=RunOutcome.TIMEOUT
        ),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
            action=PipelineAction.ENTER_POSTFLIGHT,
            outcome=RunOutcome.TIMEOUT,
        ),
    ),
    (
        "terminal_outcome_from_reviewer",
        PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=2),
        TransitionEvent(
            kind=TransitionEventKind.TERMINAL_OUTCOME,
            outcome=RunOutcome.GIT_SAFETY_ERROR,
        ),
        3,
        Transition(
            state=PipelineState(phase=PipelinePhase.POSTFLIGHT),
            action=PipelineAction.ENTER_POSTFLIGHT,
            outcome=RunOutcome.GIT_SAFETY_ERROR,
        ),
    ),
)

VALID_PAIRS = frozenset((row[1].phase, row[2].kind) for row in VALID_TRANSITIONS)
ALL_PAIRS = frozenset(
    (phase, kind) for phase in PipelinePhase for kind in TransitionEventKind
)
INVALID_PAIRS = sorted(
    ALL_PAIRS - VALID_PAIRS,
    key=lambda pair: (pair[0].value, pair[1].value),
)


@pytest.mark.parametrize(
    ("state", "event", "max_review_cycles", "expected"),
    [row[1:] for row in VALID_TRANSITIONS],
    ids=[row[0] for row in VALID_TRANSITIONS],
)
def test_valid_transitions_produce_the_expected_state_and_action(
    state: PipelineState,
    event: TransitionEvent,
    max_review_cycles: int,
    expected: Transition,
) -> None:
    assert transition(state, event, max_review_cycles=max_review_cycles) == expected


@pytest.mark.parametrize(
    ("phase", "kind"),
    INVALID_PAIRS,
    ids=[f"{phase.value}--{kind.value}" for phase, kind in INVALID_PAIRS],
)
def test_every_other_phase_event_pair_is_rejected(
    phase: PipelinePhase,
    kind: TransitionEventKind,
) -> None:
    state = _state_for(phase)
    outcome = (
        RunOutcome.PROCESS_ERROR
        if kind is TransitionEventKind.TERMINAL_OUTCOME
        else None
    )
    event = TransitionEvent(kind=kind, outcome=outcome)

    with pytest.raises(ValueError, match="is not a valid transition"):
        transition(state, event, max_review_cycles=3)


def test_exhaustive_matrix_covers_every_phase_and_event_kind() -> None:
    assert len(VALID_PAIRS) + len(INVALID_PAIRS) == len(ALL_PAIRS)
    assert len(PipelinePhase) == 7
    assert len(TransitionEventKind) == 8


def test_review_cycle_starts_at_one_and_advances_only_after_changes_required() -> None:
    state = PipelineState(phase=PipelinePhase.PREFLIGHT)

    step = transition(
        state,
        TransitionEvent(kind=TransitionEventKind.PREFLIGHT_SUCCEEDED),
        max_review_cycles=5,
    )
    assert step.state.phase is PipelinePhase.ARCHITECT

    step = transition(
        step.state,
        TransitionEvent(kind=TransitionEventKind.ARCHITECT_READY),
        max_review_cycles=5,
    )
    assert step.state == PipelineState(phase=PipelinePhase.CODER, review_cycle=1)

    step = transition(
        step.state,
        TransitionEvent(kind=TransitionEventKind.CODER_COMPLETED),
        max_review_cycles=5,
    )
    assert step.state == PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=1)

    step = transition(
        step.state,
        TransitionEvent(kind=TransitionEventKind.REVIEWER_CHANGES_REQUIRED),
        max_review_cycles=5,
    )
    assert step.state == PipelineState(phase=PipelinePhase.CODER, review_cycle=2)
    assert step.action is PipelineAction.INVOKE_CODER

    step = transition(
        step.state,
        TransitionEvent(kind=TransitionEventKind.CODER_COMPLETED),
        max_review_cycles=5,
    )
    assert step.state == PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=2)

    step = transition(
        step.state,
        TransitionEvent(kind=TransitionEventKind.REVIEWER_APPROVED),
        max_review_cycles=5,
    )
    assert step.state == PipelineState(phase=PipelinePhase.POSTFLIGHT)
    assert step.outcome is None


@pytest.mark.parametrize("max_review_cycles", [1, 2, 5])
def test_exhaustion_at_the_configured_limit_never_invokes_another_coder(
    max_review_cycles: int,
) -> None:
    state = PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=max_review_cycles)

    result = transition(
        state,
        TransitionEvent(kind=TransitionEventKind.REVIEWER_CHANGES_REQUIRED),
        max_review_cycles=max_review_cycles,
    )

    assert result.action is not PipelineAction.INVOKE_CODER
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.REVIEW_CYCLES_EXHAUSTED
    assert result.state.phase is PipelinePhase.POSTFLIGHT


@pytest.mark.parametrize(("cycle", "max_review_cycles"), [(1, 3), (2, 3), (1, 2)])
def test_changes_required_below_the_limit_always_invokes_a_coder(
    cycle: int,
    max_review_cycles: int,
) -> None:
    state = PipelineState(phase=PipelinePhase.REVIEWER, review_cycle=cycle)

    result = transition(
        state,
        TransitionEvent(kind=TransitionEventKind.REVIEWER_CHANGES_REQUIRED),
        max_review_cycles=max_review_cycles,
    )

    assert result.action is PipelineAction.INVOKE_CODER
    assert result.outcome is None
    assert result.state == PipelineState(
        phase=PipelinePhase.CODER, review_cycle=cycle + 1
    )


def test_terminal_outcome_does_not_change_the_review_cycle_counter() -> None:
    state = PipelineState(phase=PipelinePhase.CODER, review_cycle=2)

    result = transition(
        state,
        TransitionEvent(
            kind=TransitionEventKind.TERMINAL_OUTCOME, outcome=RunOutcome.TIMEOUT
        ),
        max_review_cycles=5,
    )

    assert result.state == PipelineState(phase=PipelinePhase.POSTFLIGHT)
    assert result.outcome is RunOutcome.TIMEOUT


def test_repeated_traces_with_identical_events_are_deterministic() -> None:
    events = (
        TransitionEvent(kind=TransitionEventKind.PREFLIGHT_SUCCEEDED),
        TransitionEvent(kind=TransitionEventKind.ARCHITECT_READY),
        TransitionEvent(kind=TransitionEventKind.CODER_COMPLETED),
        TransitionEvent(kind=TransitionEventKind.REVIEWER_CHANGES_REQUIRED),
        TransitionEvent(kind=TransitionEventKind.CODER_COMPLETED),
        TransitionEvent(kind=TransitionEventKind.REVIEWER_APPROVED),
        TransitionEvent(kind=TransitionEventKind.POSTFLIGHT_COMPLETED),
        TransitionEvent(kind=TransitionEventKind.FINALIZATION_COMPLETED),
    )

    def run_trace() -> tuple[Transition, ...]:
        state = PipelineState(phase=PipelinePhase.PREFLIGHT)
        trace: list[Transition] = []
        for event in events:
            result = transition(state, event, max_review_cycles=3)
            trace.append(result)
            state = result.state
        return tuple(trace)

    first_trace = run_trace()
    second_trace = run_trace()

    assert first_trace == second_trace
    assert first_trace[-1].state == PipelineState(phase=PipelinePhase.FINISHED)
    assert first_trace[-1].action is PipelineAction.FINISH


def test_transition_rejects_wrong_argument_types() -> None:
    state = PipelineState(phase=PipelinePhase.PREFLIGHT)
    event = TransitionEvent(kind=TransitionEventKind.PREFLIGHT_SUCCEEDED)

    with pytest.raises(TypeError, match="state must be PipelineState"):
        transition(cast(PipelineState, "PREFLIGHT"), event, max_review_cycles=3)

    with pytest.raises(TypeError, match="event must be TransitionEvent"):
        transition(state, cast(TransitionEvent, "READY"), max_review_cycles=3)

    with pytest.raises(TypeError, match="max_review_cycles must be an integer"):
        transition(state, event, max_review_cycles=cast(int, "3"))

    with pytest.raises(ValueError, match="max_review_cycles must be at least 1"):
        transition(state, event, max_review_cycles=0)


def test_pipeline_state_requires_a_review_cycle_only_for_coder_and_reviewer() -> None:
    with pytest.raises(ValueError, match="requires a review_cycle"):
        PipelineState(phase=PipelinePhase.CODER)

    with pytest.raises(ValueError, match="requires a review_cycle"):
        PipelineState(phase=PipelinePhase.REVIEWER)

    with pytest.raises(ValueError, match="must not carry a review_cycle"):
        PipelineState(phase=PipelinePhase.PREFLIGHT, review_cycle=1)

    with pytest.raises(ValueError, match="must not carry a review_cycle"):
        PipelineState(phase=PipelinePhase.POSTFLIGHT, review_cycle=1)


def test_pipeline_state_rejects_invalid_review_cycle_values() -> None:
    with pytest.raises(TypeError, match="review_cycle must be an integer or None"):
        PipelineState(phase=PipelinePhase.CODER, review_cycle=cast(int, "1"))

    with pytest.raises(ValueError, match="review_cycle must be at least 1"):
        PipelineState(phase=PipelinePhase.CODER, review_cycle=0)


def test_pipeline_state_rejects_a_non_pipeline_phase() -> None:
    with pytest.raises(TypeError, match="phase must be PipelinePhase"):
        PipelineState(phase=cast(PipelinePhase, RunOutcome.SUCCEEDED))


def test_transition_event_requires_an_outcome_only_for_terminal_outcome() -> None:
    with pytest.raises(TypeError, match="outcome must be RunOutcome"):
        TransitionEvent(kind=TransitionEventKind.TERMINAL_OUTCOME)

    with pytest.raises(ValueError, match="TERMINAL_OUTCOME must not be SUCCEEDED"):
        TransitionEvent(
            kind=TransitionEventKind.TERMINAL_OUTCOME,
            outcome=RunOutcome.SUCCEEDED,
        )

    with pytest.raises(ValueError, match="must not carry an outcome"):
        TransitionEvent(
            kind=TransitionEventKind.ARCHITECT_READY,
            outcome=RunOutcome.PROCESS_ERROR,
        )


def test_transition_event_rejects_a_non_event_kind() -> None:
    with pytest.raises(TypeError, match="kind must be TransitionEventKind"):
        TransitionEvent(kind=cast(TransitionEventKind, "READY"))


def test_canonical_str_enum_members_and_values() -> None:
    expected: dict[type[StrEnum], tuple[str, ...]] = {
        TransitionEventKind: (
            "PREFLIGHT_SUCCEEDED",
            "ARCHITECT_READY",
            "CODER_COMPLETED",
            "REVIEWER_APPROVED",
            "REVIEWER_CHANGES_REQUIRED",
            "TERMINAL_OUTCOME",
            "POSTFLIGHT_COMPLETED",
            "FINALIZATION_COMPLETED",
        ),
        PipelineAction: (
            "INVOKE_ARCHITECT",
            "INVOKE_CODER",
            "INVOKE_REVIEWER",
            "ENTER_POSTFLIGHT",
            "ENTER_FINALIZATION",
            "FINISH",
        ),
    }

    for enum_type, values in expected.items():
        assert tuple(member.name for member in enum_type) == values
        assert tuple(member.value for member in enum_type) == values
        with pytest.raises(ValueError):
            enum_type("UNKNOWN")


def test_state_event_and_transition_records_are_frozen_and_slotted() -> None:
    state = PipelineState(phase=PipelinePhase.PREFLIGHT)
    event = TransitionEvent(kind=TransitionEventKind.PREFLIGHT_SUCCEEDED)
    result = Transition(state=state, action=PipelineAction.INVOKE_ARCHITECT)

    for record in (state, event, result):
        assert not hasattr(record, "__dict__")
        first_field = next(iter(record.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(record, first_field, getattr(record, first_field))


def test_transition_rejects_wrong_field_types() -> None:
    state = PipelineState(phase=PipelinePhase.PREFLIGHT)

    with pytest.raises(TypeError, match="state must be PipelineState"):
        Transition(state=cast(PipelineState, "PREFLIGHT"), action=PipelineAction.FINISH)

    with pytest.raises(TypeError, match="action must be PipelineAction"):
        Transition(state=state, action=cast(PipelineAction, "FINISH"))

    with pytest.raises(TypeError, match="outcome must be RunOutcome"):
        Transition(
            state=state,
            action=PipelineAction.ENTER_POSTFLIGHT,
            outcome=cast(RunOutcome, "TIMEOUT"),
        )

"""Unit tests for attempt/pipeline precedence, the final gate, and exit codes."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from opencode_tools.domain import (
    FinalStatus,
    GitSafetyStatus,
    PersistenceStatus,
    ReviewStatus,
    RunOutcome,
)
from opencode_tools.state_machine import (
    AttemptPrecedence,
    TerminalPrecedence,
    classify_attempt_outcome,
    evaluate_final_gate,
    resolve_exit_code,
    resolve_terminal_outcome,
)

# --- Attempt precedence (System Design SS13.2) ------------------------------

ALL_FALSE_SIGNALS: dict[str, bool] = {
    "timed_out": False,
    "provider_error": False,
    "process_error": False,
    "protocol_error": False,
    "agent_reported_failure": False,
    "succeeded": False,
}


def _signals(**overrides: bool) -> dict[str, bool]:
    signals = dict(ALL_FALSE_SIGNALS)
    signals.update(overrides)
    return signals


ATTEMPT_PRECEDENCE_CASES: tuple[tuple[str, dict[str, bool], RunOutcome], ...] = (
    ("timeout_alone", _signals(timed_out=True), RunOutcome.TIMEOUT),
    ("provider_alone", _signals(provider_error=True), RunOutcome.PROVIDER_ERROR),
    ("process_alone", _signals(process_error=True), RunOutcome.PROCESS_ERROR),
    ("protocol_alone", _signals(protocol_error=True), RunOutcome.PROTOCOL_ERROR),
    (
        "agent_failure_alone",
        _signals(agent_reported_failure=True),
        RunOutcome.AGENT_REPORTED_FAILURE,
    ),
    ("succeeded_alone", _signals(succeeded=True), RunOutcome.SUCCEEDED),
    (
        "timeout_beats_everything",
        _signals(
            timed_out=True,
            provider_error=True,
            process_error=True,
            protocol_error=True,
            agent_reported_failure=True,
            succeeded=True,
        ),
        RunOutcome.TIMEOUT,
    ),
    (
        "provider_beats_process_and_below",
        _signals(provider_error=True, process_error=True, succeeded=True),
        RunOutcome.PROVIDER_ERROR,
    ),
    (
        "process_beats_protocol_and_below",
        _signals(process_error=True, protocol_error=True, succeeded=True),
        RunOutcome.PROCESS_ERROR,
    ),
    (
        "protocol_beats_agent_failure_and_below",
        _signals(protocol_error=True, agent_reported_failure=True, succeeded=True),
        RunOutcome.PROTOCOL_ERROR,
    ),
    (
        "agent_failure_beats_succeeded",
        _signals(agent_reported_failure=True, succeeded=True),
        RunOutcome.AGENT_REPORTED_FAILURE,
    ),
)


@pytest.mark.parametrize(
    ("_name", "signals", "expected"),
    ATTEMPT_PRECEDENCE_CASES,
    ids=[name for name, _, _ in ATTEMPT_PRECEDENCE_CASES],
)
def test_attempt_precedence_picks_the_highest_priority_true_signal(
    _name: str,
    signals: dict[str, bool],
    expected: RunOutcome,
) -> None:
    result = classify_attempt_outcome(**signals)

    assert result.outcome is expected
    assert result.concurrent_signals[0] is expected


def test_attempt_precedence_preserves_every_concurrent_signal_in_order() -> None:
    result = classify_attempt_outcome(
        **_signals(provider_error=True, protocol_error=True, succeeded=True)
    )

    assert result.outcome is RunOutcome.PROVIDER_ERROR
    assert result.concurrent_signals == (
        RunOutcome.PROVIDER_ERROR,
        RunOutcome.PROTOCOL_ERROR,
        RunOutcome.SUCCEEDED,
    )


def test_attempt_precedence_rejects_when_no_signal_is_true() -> None:
    with pytest.raises(ValueError, match="at least one attempt signal must be true"):
        classify_attempt_outcome(**ALL_FALSE_SIGNALS)


def test_attempt_precedence_rejects_non_boolean_signals() -> None:
    with pytest.raises(TypeError, match="timeout must be a boolean"):
        classify_attempt_outcome(**_signals(timed_out=cast(bool, "yes")))


def test_attempt_precedence_record_is_frozen_and_internally_consistent() -> None:
    record = classify_attempt_outcome(**_signals(succeeded=True))
    assert not hasattr(record, "__dict__")
    with pytest.raises(FrozenInstanceError):
        record.outcome = RunOutcome.TIMEOUT  # type: ignore[misc]

    with pytest.raises(TypeError, match="concurrent_signals must be a tuple"):
        AttemptPrecedence(
            outcome=RunOutcome.SUCCEEDED,
            concurrent_signals=cast(tuple[RunOutcome, ...], [RunOutcome.SUCCEEDED]),
        )
    with pytest.raises(ValueError, match="concurrent_signals must not be empty"):
        AttemptPrecedence(outcome=RunOutcome.SUCCEEDED, concurrent_signals=())
    with pytest.raises(ValueError, match="highest-precedence signal"):
        AttemptPrecedence(
            outcome=RunOutcome.SUCCEEDED,
            concurrent_signals=(RunOutcome.TIMEOUT, RunOutcome.SUCCEEDED),
        )


# --- Pipeline precedence (System Design SS13.3) -----------------------------


def test_pipeline_precedence_defaults_to_the_trigger_outcome_when_all_else_is_clean() -> (
    None
):
    result = resolve_terminal_outcome(
        trigger_outcome=RunOutcome.PROVIDER_ERROR,
        git_safety_status=GitSafetyStatus.SAFE,
        interrupted=False,
        persistence_status=PersistenceStatus.OK,
    )

    assert result.terminal_outcome is RunOutcome.PROVIDER_ERROR
    assert result.concurrent_causes == (RunOutcome.PROVIDER_ERROR,)


def test_pipeline_precedence_logging_error_beats_everything() -> None:
    result = resolve_terminal_outcome(
        trigger_outcome=RunOutcome.PROVIDER_ERROR,
        git_safety_status=GitSafetyStatus.UNSAFE,
        interrupted=True,
        persistence_status=PersistenceStatus.FAILED,
    )

    assert result.terminal_outcome is RunOutcome.LOGGING_ERROR
    assert result.concurrent_causes == (
        RunOutcome.PROVIDER_ERROR,
        RunOutcome.INTERRUPTED,
        RunOutcome.GIT_SAFETY_ERROR,
        RunOutcome.LOGGING_ERROR,
    )


@pytest.mark.parametrize(
    "git_safety_status",
    [GitSafetyStatus.UNSAFE, GitSafetyStatus.INDETERMINATE],
)
def test_pipeline_precedence_git_safety_beats_interrupted_and_trigger(
    git_safety_status: GitSafetyStatus,
) -> None:
    result = resolve_terminal_outcome(
        trigger_outcome=RunOutcome.SUCCEEDED,
        git_safety_status=git_safety_status,
        interrupted=True,
        persistence_status=PersistenceStatus.OK,
    )

    assert result.terminal_outcome is RunOutcome.GIT_SAFETY_ERROR


def test_pipeline_precedence_interrupted_beats_the_trigger_outcome() -> None:
    result = resolve_terminal_outcome(
        trigger_outcome=RunOutcome.TIMEOUT,
        git_safety_status=GitSafetyStatus.SAFE,
        interrupted=True,
        persistence_status=PersistenceStatus.OK,
    )

    assert result.terminal_outcome is RunOutcome.INTERRUPTED
    assert result.concurrent_causes == (RunOutcome.TIMEOUT, RunOutcome.INTERRUPTED)


def test_pipeline_precedence_approval_does_not_survive_postflight_git_drift() -> None:
    # AC-019: reviewer APPROVED followed by Git drift still terminates FAILED.
    result = resolve_terminal_outcome(
        trigger_outcome=RunOutcome.SUCCEEDED,
        git_safety_status=GitSafetyStatus.UNSAFE,
        interrupted=False,
        persistence_status=PersistenceStatus.OK,
    )

    assert result.terminal_outcome is RunOutcome.GIT_SAFETY_ERROR
    assert RunOutcome.SUCCEEDED in result.concurrent_causes


def test_pipeline_precedence_deduplicates_a_cause_matching_the_trigger() -> None:
    result = resolve_terminal_outcome(
        trigger_outcome=RunOutcome.GIT_SAFETY_ERROR,
        git_safety_status=GitSafetyStatus.UNSAFE,
        interrupted=False,
        persistence_status=PersistenceStatus.OK,
    )

    assert result.concurrent_causes == (RunOutcome.GIT_SAFETY_ERROR,)


def test_pipeline_precedence_rejects_wrong_argument_types() -> None:
    with pytest.raises(TypeError, match="trigger_outcome must be RunOutcome"):
        resolve_terminal_outcome(
            trigger_outcome=cast(RunOutcome, "TIMEOUT"),
            git_safety_status=GitSafetyStatus.SAFE,
            interrupted=False,
            persistence_status=PersistenceStatus.OK,
        )
    with pytest.raises(TypeError, match="interrupted must be a boolean"):
        resolve_terminal_outcome(
            trigger_outcome=RunOutcome.SUCCEEDED,
            git_safety_status=GitSafetyStatus.SAFE,
            interrupted=cast(bool, "no"),
            persistence_status=PersistenceStatus.OK,
        )


def test_terminal_precedence_record_rejects_empty_or_wrong_typed_causes() -> None:
    with pytest.raises(TypeError, match="concurrent_causes must be a tuple"):
        TerminalPrecedence(
            terminal_outcome=RunOutcome.TIMEOUT,
            concurrent_causes=cast(tuple[RunOutcome, ...], [RunOutcome.TIMEOUT]),
        )
    with pytest.raises(ValueError, match="concurrent_causes must not be empty"):
        TerminalPrecedence(terminal_outcome=RunOutcome.TIMEOUT, concurrent_causes=())


# --- Final gate (System Design SS8.4) ---------------------------------------


def _gate_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "review_status": ReviewStatus.APPROVED,
        "postflight_git_safety_status": GitSafetyStatus.SAFE,
        "baseline_branch": "feat/domain",
        "baseline_head": "abc123",
        "postflight_branch": "feat/domain",
        "postflight_head": "abc123",
        "persistence_status": PersistenceStatus.OK,
    }
    kwargs.update(overrides)
    return kwargs


def _evaluate(**overrides: object) -> FinalStatus:
    return evaluate_final_gate(**_gate_kwargs(**overrides))  # type: ignore[arg-type]


def test_final_gate_approves_only_when_every_condition_holds() -> None:
    assert _evaluate() is FinalStatus.APPROVED


GATE_DENIAL_CASES: tuple[tuple[str, dict[str, object]], ...] = (
    ("no_review_decision", {"review_status": None}),
    ("changes_required", {"review_status": ReviewStatus.CHANGES_REQUIRED}),
    (
        "postflight_unsafe",
        {"postflight_git_safety_status": GitSafetyStatus.UNSAFE},
    ),
    (
        "postflight_indeterminate",
        {"postflight_git_safety_status": GitSafetyStatus.INDETERMINATE},
    ),
    ("branch_drift", {"postflight_branch": "main"}),
    ("head_drift", {"postflight_head": "def456"}),
    ("missing_baseline_branch", {"baseline_branch": None}),
    ("missing_baseline_head", {"baseline_head": None}),
    ("missing_postflight_branch", {"postflight_branch": None}),
    ("missing_postflight_head", {"postflight_head": None}),
    ("persistence_failed", {"persistence_status": PersistenceStatus.FAILED}),
    ("persistence_incomplete", {"persistence_status": PersistenceStatus.INCOMPLETE}),
)


@pytest.mark.parametrize(
    ("_name", "override"),
    GATE_DENIAL_CASES,
    ids=[name for name, _ in GATE_DENIAL_CASES],
)
def test_any_single_failing_condition_denies_approval(
    _name: str,
    override: dict[str, object],
) -> None:
    assert _evaluate(**override) is FinalStatus.FAILED


def test_final_gate_rejects_wrong_argument_types() -> None:
    with pytest.raises(TypeError, match="review_status must be ReviewStatus"):
        _evaluate(review_status=cast(ReviewStatus, "APPROVED"))
    with pytest.raises(
        TypeError,
        match="postflight_git_safety_status must be GitSafetyStatus",
    ):
        _evaluate(postflight_git_safety_status=cast(GitSafetyStatus, "SAFE"))
    with pytest.raises(TypeError, match="baseline_branch must be a string or None"):
        _evaluate(baseline_branch=cast(str, 123))
    with pytest.raises(TypeError, match="persistence_status must be PersistenceStatus"):
        _evaluate(persistence_status=cast(PersistenceStatus, "OK"))


# --- Exit-code mapping (System Design SS13.4) -------------------------------


def test_approved_always_maps_to_exit_code_zero() -> None:
    for outcome in RunOutcome:
        assert (
            resolve_exit_code(
                final_status=FinalStatus.APPROVED, terminal_outcome=outcome
            )
            == 0
        )


EXIT_CODE_FAMILY: dict[RunOutcome, int] = {
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


def test_exit_code_mapping_is_total_and_matches_the_canonical_families() -> None:
    assert set(EXIT_CODE_FAMILY) == set(RunOutcome)
    for outcome, expected_code in EXIT_CODE_FAMILY.items():
        assert (
            resolve_exit_code(final_status=FinalStatus.FAILED, terminal_outcome=outcome)
            == expected_code
        )


def test_failed_never_maps_to_exit_code_zero() -> None:
    # AC-025: final status and exit code always agree.
    for outcome in RunOutcome:
        assert (
            resolve_exit_code(final_status=FinalStatus.FAILED, terminal_outcome=outcome)
            != 0
        )


def test_resolve_exit_code_rejects_wrong_argument_types() -> None:
    with pytest.raises(TypeError, match="final_status must be FinalStatus"):
        resolve_exit_code(
            final_status=cast(FinalStatus, "APPROVED"),
            terminal_outcome=RunOutcome.SUCCEEDED,
        )
    with pytest.raises(TypeError, match="terminal_outcome must be RunOutcome"):
        resolve_exit_code(
            final_status=FinalStatus.FAILED,
            terminal_outcome=cast(RunOutcome, "TIMEOUT"),
        )


# --- End-to-end composition with the state machine (SS8.1 + SS8.4) ---------


def test_provider_exhaustion_reaches_postflight_and_maps_to_a_handled_exit_family() -> (
    None
):
    # AC-012: provider exhaustion terminates FAILED/PROVIDER_ERROR.
    pipeline = resolve_terminal_outcome(
        trigger_outcome=RunOutcome.PROVIDER_ERROR,
        git_safety_status=GitSafetyStatus.SAFE,
        interrupted=False,
        persistence_status=PersistenceStatus.OK,
    )
    final_status = evaluate_final_gate(
        review_status=None,
        postflight_git_safety_status=GitSafetyStatus.SAFE,
        baseline_branch="feat/domain",
        baseline_head="abc123",
        postflight_branch="feat/domain",
        postflight_head="abc123",
        persistence_status=PersistenceStatus.OK,
    )
    exit_code = resolve_exit_code(
        final_status=final_status,
        terminal_outcome=pipeline.terminal_outcome,
    )

    assert pipeline.terminal_outcome is RunOutcome.PROVIDER_ERROR
    assert final_status is FinalStatus.FAILED
    assert exit_code == 20

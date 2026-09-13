"""Unit tests for the pure provider-retry policy and theoretical bound."""

from __future__ import annotations

import inspect
from typing import cast

import pytest

from opencode_tools.domain import (
    AgentRole,
    ExecutionConfig,
    GitSafetyStatus,
    PersistenceStatus,
    ProviderDiagnostic,
    ProviderRetryConfig,
    RetryDecision,
    RunOutcome,
)
from opencode_tools.retry import (
    backoff_budget_seconds,
    compute_backoff_delay_seconds,
    decide_retry,
    logical_invocation_count,
    max_opencode_invocations,
    theoretical_opencode_and_backoff_bound_seconds,
)

DEFAULT_RETRY_CONFIG = ProviderRetryConfig(
    max_attempts=3,
    initial_delay_seconds=2,
    multiplier=2.0,
    max_delay_seconds=30,
)
DEFAULT_EXECUTION_CONFIG = ExecutionConfig(
    opencode_timeout_seconds=1800,
    utility_timeout_seconds=30,
    termination_grace_seconds=5,
    max_review_cycles=3,
)


def _retryable_diagnostic() -> ProviderDiagnostic:
    return ProviderDiagnostic(source="opencode", signature="http-429", retryable=True)


def _authorized_kwargs() -> dict[str, object]:
    return {
        "outcome": RunOutcome.PROVIDER_ERROR,
        "provider_diagnostic": _retryable_diagnostic(),
        "provider_attempt": 1,
        "role": AgentRole.CODER,
        "target_changed": False,
        "termination_confirmed": True,
        "git_safety_status": GitSafetyStatus.SAFE,
        "persistence_status": PersistenceStatus.OK,
        "cancellation_requested": False,
        "config": DEFAULT_RETRY_CONFIG,
    }


def _decide(**overrides: object) -> RetryDecision:
    kwargs = _authorized_kwargs()
    kwargs.update(overrides)
    return decide_retry(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 2.0), (2, 4.0), (3, 8.0), (4, 16.0), (5, 30.0), (6, 30.0)],
)
def test_backoff_delay_matches_the_canonical_capped_formula(
    attempt: int,
    expected: float,
) -> None:
    assert compute_backoff_delay_seconds(attempt, DEFAULT_RETRY_CONFIG) == expected


def test_backoff_delay_rejects_invalid_attempt_or_config() -> None:
    with pytest.raises(ValueError, match="attempt must be at least 1"):
        compute_backoff_delay_seconds(0, DEFAULT_RETRY_CONFIG)
    with pytest.raises(TypeError, match="attempt must be an integer"):
        compute_backoff_delay_seconds(cast(int, "1"), DEFAULT_RETRY_CONFIG)
    with pytest.raises(TypeError, match="config must be ProviderRetryConfig"):
        compute_backoff_delay_seconds(1, cast(ProviderRetryConfig, "config"))


def test_all_guards_true_authorizes_a_retry_with_the_planned_delay() -> None:
    decision = _decide()

    assert decision == RetryDecision(
        should_retry=True,
        next_provider_attempt=2,
        planned_delay_seconds=2.0,
        retry_suppressed_due_to_target_change=False,
    )


def test_a_retry_never_reuses_a_previous_attempt_number() -> None:
    first = _decide(provider_attempt=1)
    second = _decide(provider_attempt=2)

    assert first.next_provider_attempt == 2
    assert second.next_provider_attempt == 3
    assert first.next_provider_attempt != second.next_provider_attempt


GUARD_VIOLATIONS: tuple[tuple[str, dict[str, object]], ...] = (
    ("non_provider_outcome", {"outcome": RunOutcome.TIMEOUT}),
    (
        "non_retryable_diagnostic",
        {
            "provider_diagnostic": ProviderDiagnostic(
                source="opencode",
                signature="http-400",
                retryable=False,
            ),
        },
    ),
    ("missing_diagnostic", {"provider_diagnostic": None}),
    ("attempt_budget_exhausted", {"provider_attempt": 3}),
    ("termination_not_confirmed", {"termination_confirmed": False}),
    ("termination_unknown", {"termination_confirmed": None}),
    ("git_unsafe", {"git_safety_status": GitSafetyStatus.UNSAFE}),
    ("git_indeterminate", {"git_safety_status": GitSafetyStatus.INDETERMINATE}),
    ("persistence_failed", {"persistence_status": PersistenceStatus.FAILED}),
    ("persistence_incomplete", {"persistence_status": PersistenceStatus.INCOMPLETE}),
    ("cancellation_requested", {"cancellation_requested": True}),
    ("coder_target_changed", {"role": AgentRole.CODER, "target_changed": True}),
)


@pytest.mark.parametrize(
    ("_name", "override"),
    GUARD_VIOLATIONS,
    ids=[name for name, _ in GUARD_VIOLATIONS],
)
def test_any_single_failing_guard_denies_the_retry(
    _name: str,
    override: dict[str, object],
) -> None:
    decision = _decide(**override)

    assert decision.should_retry is False
    assert decision.next_provider_attempt is None
    assert decision.planned_delay_seconds is None


def test_provider_exhaustion_keeps_the_outcome_and_denies_further_retries() -> None:
    decision = _decide(provider_attempt=DEFAULT_RETRY_CONFIG.max_attempts)

    assert decision.should_retry is False


def test_target_change_is_only_suppressive_for_the_coder_role() -> None:
    coder_changed = _decide(role=AgentRole.CODER, target_changed=True)
    assert coder_changed.should_retry is False
    assert coder_changed.retry_suppressed_due_to_target_change is True

    reviewer_changed = _decide(role=AgentRole.REVIEWER, target_changed=True)
    assert reviewer_changed.should_retry is True
    assert reviewer_changed.retry_suppressed_due_to_target_change is False

    architect_changed = _decide(role=AgentRole.ARCHITECT, target_changed=True)
    assert architect_changed.should_retry is True
    assert architect_changed.retry_suppressed_due_to_target_change is False

    coder_unchanged = _decide(role=AgentRole.CODER, target_changed=False)
    assert coder_unchanged.should_retry is True
    assert coder_unchanged.retry_suppressed_due_to_target_change is False


def test_target_change_flag_is_independent_of_other_guard_failures() -> None:
    decision = _decide(
        role=AgentRole.CODER,
        target_changed=True,
        outcome=RunOutcome.TIMEOUT,
    )

    assert decision.should_retry is False
    assert decision.retry_suppressed_due_to_target_change is True


def test_decide_retry_rejects_wrong_argument_types() -> None:
    with pytest.raises(TypeError, match="outcome must be RunOutcome"):
        _decide(outcome=cast(RunOutcome, "PROVIDER_ERROR"))
    with pytest.raises(TypeError, match="provider_diagnostic must be"):
        _decide(provider_diagnostic=cast(ProviderDiagnostic, "diagnostic"))
    with pytest.raises(TypeError, match="provider_attempt must be an integer"):
        _decide(provider_attempt=cast(int, "1"))
    with pytest.raises(ValueError, match="provider_attempt must be at least 1"):
        _decide(provider_attempt=0)
    with pytest.raises(TypeError, match="role must be AgentRole"):
        _decide(role=cast(AgentRole, "CODER"))
    with pytest.raises(TypeError, match="target_changed must be a boolean"):
        _decide(target_changed=cast(bool, "false"))
    with pytest.raises(
        TypeError, match="termination_confirmed must be a boolean or None"
    ):
        _decide(termination_confirmed=cast(bool, "true"))
    with pytest.raises(TypeError, match="git_safety_status must be GitSafetyStatus"):
        _decide(git_safety_status=cast(GitSafetyStatus, "SAFE"))
    with pytest.raises(TypeError, match="persistence_status must be PersistenceStatus"):
        _decide(persistence_status=cast(PersistenceStatus, "OK"))
    with pytest.raises(TypeError, match="cancellation_requested must be a boolean"):
        _decide(cancellation_requested=cast(bool, "false"))
    with pytest.raises(TypeError, match="config must be ProviderRetryConfig"):
        _decide(config=cast(ProviderRetryConfig, "config"))


def test_retry_policy_never_touches_the_review_cycle_counter() -> None:
    decide_retry_parameters = inspect.signature(decide_retry).parameters
    retry_decision_fields = RetryDecision.__dataclass_fields__

    assert "review_cycle" not in decide_retry_parameters
    assert "review_cycle" not in retry_decision_fields


def test_logical_invocation_and_opencode_invocation_counts() -> None:
    assert logical_invocation_count(3) == 7
    assert logical_invocation_count(1) == 3
    assert max_opencode_invocations(max_review_cycles=3, max_provider_attempts=3) == 21
    assert max_opencode_invocations(max_review_cycles=1, max_provider_attempts=1) == 3


def test_logical_invocation_count_rejects_invalid_input() -> None:
    with pytest.raises(ValueError, match="max_review_cycles must be at least 1"):
        logical_invocation_count(0)


def test_backoff_budget_sums_every_delay_before_the_last_attempt() -> None:
    assert backoff_budget_seconds(DEFAULT_RETRY_CONFIG) == 6.0

    single_attempt_config = ProviderRetryConfig(
        max_attempts=1,
        initial_delay_seconds=2,
        multiplier=2.0,
        max_delay_seconds=30,
    )
    assert backoff_budget_seconds(single_attempt_config) == 0.0


def test_theoretical_bound_matches_the_configured_default_values() -> None:
    bound = theoretical_opencode_and_backoff_bound_seconds(
        DEFAULT_EXECUTION_CONFIG,
        DEFAULT_RETRY_CONFIG,
    )

    # P=21, per-call=1810s -> 38010; L=7, B(A)=6 -> 42; total 38052s.
    assert bound == 38_052.0


def test_theoretical_bound_at_the_minimum_configuration() -> None:
    minimal_execution = ExecutionConfig(
        opencode_timeout_seconds=1,
        utility_timeout_seconds=1,
        termination_grace_seconds=0.1,
        max_review_cycles=1,
    )
    minimal_retry = ProviderRetryConfig(
        max_attempts=1,
        initial_delay_seconds=0.1,
        multiplier=1.5,
        max_delay_seconds=0.1,
    )

    bound = theoretical_opencode_and_backoff_bound_seconds(
        minimal_execution, minimal_retry
    )

    # L=3, P=3, B(A)=0 (a single attempt never backs off) -> 3*(1+0.2) = 3.6.
    assert bound == pytest.approx(3.6)


def test_theoretical_bound_rejects_wrong_argument_types() -> None:
    with pytest.raises(TypeError, match="execution must be ExecutionConfig"):
        theoretical_opencode_and_backoff_bound_seconds(
            cast(ExecutionConfig, "execution"),
            DEFAULT_RETRY_CONFIG,
        )
    with pytest.raises(TypeError, match="provider_retry must be ProviderRetryConfig"):
        theoretical_opencode_and_backoff_bound_seconds(
            DEFAULT_EXECUTION_CONFIG,
            cast(ProviderRetryConfig, "provider_retry"),
        )

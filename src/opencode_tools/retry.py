"""Pure provider-retry decisions, capped backoff, and the theoretical bound.

This module classifies whether a provider attempt may be retried and, if so,
plans its delay; it never sleeps, never classifies raw OpenCode output, never
retries a non-provider outcome, and never touches the review cycle. The
review loop (System Design SS12.3) and the coder/reviewer phase transitions
live in `state_machine.py`; this module only ever advances `provider_attempt`
within the same logical invocation (ADR-004).
"""

from __future__ import annotations

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


def _require_int(value: object, field_name: str, *, minimum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")


def _require_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a boolean")


def compute_backoff_delay_seconds(attempt: int, config: ProviderRetryConfig) -> float:
    """Return the capped exponential delay planned after provider `attempt`.

    `delay(k) = min(initial_delay_seconds * multiplier ** (k - 1),
    max_delay_seconds)` (System Design SS12.2); there is no jitter, so this
    function is exact and deterministic for a given `attempt` and `config`.
    """

    _require_int(attempt, "attempt", minimum=1)
    if type(config) is not ProviderRetryConfig:
        raise TypeError("config must be ProviderRetryConfig")

    raw_delay = config.initial_delay_seconds * (config.multiplier ** (attempt - 1))
    return min(raw_delay, config.max_delay_seconds)


def decide_retry(
    *,
    outcome: RunOutcome,
    provider_diagnostic: ProviderDiagnostic | None,
    provider_attempt: int,
    role: AgentRole,
    target_changed: bool,
    termination_confirmed: bool | None,
    git_safety_status: GitSafetyStatus,
    persistence_status: PersistenceStatus,
    cancellation_requested: bool,
    config: ProviderRetryConfig,
) -> RetryDecision:
    """Apply every canonical retry guard (System Design SS12.2) and decide.

    A retry is authorized only when `outcome` is a trusted, retryable
    `PROVIDER_ERROR`, the attempt budget is not exhausted, termination was
    confirmed, Git is `SAFE`, persistence is `OK`, a coder's target
    fingerprint did not change, and no cancellation was requested. Every
    other outcome (timeout, process/protocol error, agent-reported failure,
    Git safety error, logging error, interrupt, review exhaustion) is never
    retried by this policy.
    """

    if type(outcome) is not RunOutcome:
        raise TypeError("outcome must be RunOutcome")
    if (
        provider_diagnostic is not None
        and type(provider_diagnostic) is not ProviderDiagnostic
    ):
        raise TypeError("provider_diagnostic must be ProviderDiagnostic or None")
    _require_int(provider_attempt, "provider_attempt", minimum=1)
    if type(role) is not AgentRole:
        raise TypeError("role must be AgentRole")
    _require_bool(target_changed, "target_changed")
    if termination_confirmed is not None and type(termination_confirmed) is not bool:
        raise TypeError("termination_confirmed must be a boolean or None")
    if type(git_safety_status) is not GitSafetyStatus:
        raise TypeError("git_safety_status must be GitSafetyStatus")
    if type(persistence_status) is not PersistenceStatus:
        raise TypeError("persistence_status must be PersistenceStatus")
    _require_bool(cancellation_requested, "cancellation_requested")
    if type(config) is not ProviderRetryConfig:
        raise TypeError("config must be ProviderRetryConfig")

    target_change_blocks_retry = role is AgentRole.CODER and target_changed

    authorized = (
        outcome is RunOutcome.PROVIDER_ERROR
        and provider_diagnostic is not None
        and provider_diagnostic.retryable
        and provider_attempt < config.max_attempts
        and termination_confirmed is True
        and git_safety_status is GitSafetyStatus.SAFE
        and persistence_status is PersistenceStatus.OK
        and not target_change_blocks_retry
        and not cancellation_requested
    )

    if authorized:
        return RetryDecision(
            should_retry=True,
            next_provider_attempt=provider_attempt + 1,
            planned_delay_seconds=compute_backoff_delay_seconds(
                provider_attempt, config
            ),
            retry_suppressed_due_to_target_change=False,
        )

    return RetryDecision(
        should_retry=False,
        next_provider_attempt=None,
        planned_delay_seconds=None,
        retry_suppressed_due_to_target_change=target_change_blocks_retry,
    )


def logical_invocation_count(max_review_cycles: int) -> int:
    """Return `L = 1 + 2C`: one architect plus a coder/reviewer per cycle."""

    _require_int(max_review_cycles, "max_review_cycles", minimum=1)
    return 1 + 2 * max_review_cycles


def max_opencode_invocations(max_review_cycles: int, max_provider_attempts: int) -> int:
    """Return `P = A x L`, the worst-case number of `opencode run` calls."""

    _require_int(max_provider_attempts, "max_provider_attempts", minimum=1)
    return max_provider_attempts * logical_invocation_count(max_review_cycles)


def backoff_budget_seconds(config: ProviderRetryConfig) -> float:
    """Return `B(A) = sum(k=1..A-1, delay(k))`, the per-invocation backoff cap."""

    if type(config) is not ProviderRetryConfig:
        raise TypeError("config must be ProviderRetryConfig")
    return sum(
        compute_backoff_delay_seconds(attempt, config)
        for attempt in range(1, config.max_attempts)
    )


def theoretical_opencode_and_backoff_bound_seconds(
    execution: ExecutionConfig,
    provider_retry: ProviderRetryConfig,
) -> float:
    """Return the configured OpenCode-run-plus-backoff portion of the bound.

    This is `P * (opencode_timeout_seconds + 2 * termination_grace_seconds)
    + L * B(A)` from System Design SS12.4. It deliberately excludes `U(P)`,
    the utility-call term, and the filesystem-snapshot overhead: both are
    contributed by adapters (Git safety, GitHub, run store) not yet built,
    and System Design SS12.4 itself defers `U(P)` to "the implementation".
    """

    if type(execution) is not ExecutionConfig:
        raise TypeError("execution must be ExecutionConfig")
    if type(provider_retry) is not ProviderRetryConfig:
        raise TypeError("provider_retry must be ProviderRetryConfig")

    invocation_count = logical_invocation_count(execution.max_review_cycles)
    opencode_call_count = max_opencode_invocations(
        execution.max_review_cycles,
        provider_retry.max_attempts,
    )
    per_call_seconds = execution.opencode_timeout_seconds + (
        2 * execution.termination_grace_seconds
    )
    return (
        opencode_call_count * per_call_seconds
        + invocation_count * backoff_budget_seconds(provider_retry)
    )


__all__ = (
    "backoff_budget_seconds",
    "compute_backoff_delay_seconds",
    "decide_retry",
    "logical_invocation_count",
    "max_opencode_invocations",
    "theoretical_opencode_and_backoff_bound_seconds",
)

"""Unit tests for the pure run ID format and attempt-log naming (M08-01).

Directory creation, collision retry, and the exclusive/anti-symlink file
primitive touch the real filesystem and live in
`tests/component/test_runtime_store.py` instead.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta, timezone

import pytest

from opencode_tools.domain import AgentRole
from opencode_tools.runlog import (
    RUN_ID_PATTERN,
    attempt_log_filename,
    format_run_id,
    generate_run_id,
)


class FakeClock:
    """A `Clock` fake returning a fixed wall-clock time."""

    def __init__(self, *, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        raise NotImplementedError("generate_run_id never needs monotonic time")


NOW = datetime(2026, 9, 11, 14, 23, 45, 123456, tzinfo=UTC)


def test_format_run_id_builds_the_canonical_compact_timestamp_and_suffix() -> None:
    run_id = format_run_id(NOW, "a1b2c3d4e5f6")

    assert run_id == "20260911T142345.123456Z-a1b2c3d4e5f6"


def test_format_run_id_rejects_a_naive_datetime() -> None:
    naive = datetime(2026, 9, 11, 14, 23, 45)  # noqa: DTZ001 - invalid fixture
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        format_run_id(naive, "a1b2c3d4e5f6")


def test_format_run_id_rejects_a_non_utc_offset() -> None:
    shifted = datetime(2026, 9, 11, 14, 23, 45, tzinfo=timezone(timedelta(hours=5)))

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        format_run_id(shifted, "a1b2c3d4e5f6")


@pytest.mark.parametrize(
    "suffix",
    ["A1B2C3D4E5F6", "a1b2c3d4e5f", "a1b2c3d4e5f6a", "", "not-hex-value"],
)
def test_format_run_id_rejects_a_malformed_suffix(suffix: str) -> None:
    with pytest.raises(ValueError, match="12 lowercase hex"):
        format_run_id(NOW, suffix)


def test_format_run_id_rejects_a_non_datetime_moment() -> None:
    with pytest.raises(TypeError, match="moment must be a datetime"):
        format_run_id("2026-09-11T14:23:45Z", "a1b2c3d4e5f6")  # type: ignore[arg-type]


def test_generate_run_id_combines_the_clock_and_a_random_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secrets, "token_hex", lambda length: "a" * (length * 2))

    run_id = generate_run_id(FakeClock(now=NOW))

    assert run_id == "20260911T142345.123456Z-" + "a" * 12
    assert RUN_ID_PATTERN.fullmatch(run_id)


def test_generate_run_id_produces_a_pattern_matching_id_with_real_randomness() -> None:
    run_id = generate_run_id(FakeClock(now=NOW))

    assert RUN_ID_PATTERN.fullmatch(run_id)


def test_generate_run_id_draws_a_fresh_suffix_each_call() -> None:
    clock = FakeClock(now=NOW)

    first = generate_run_id(clock)
    second = generate_run_id(clock)

    assert first != second


@pytest.mark.parametrize(
    ("run_id", "expected"),
    [
        ("20260911T142345.123456Z-a1b2c3d4e5f6", True),
        ("20260911T142345.123456Z-A1B2C3D4E5F6", False),
        ("2026091T142345.123456Z-a1b2c3d4e5f6", False),
        ("20260911T142345.12345Z-a1b2c3d4e5f6", False),
        ("20260911T142345.123456-a1b2c3d4e5f6", False),
        ("20260911T142345.123456Z-a1b2c3d4e5f", False),
    ],
)
def test_run_id_pattern_matches_only_the_canonical_shape(
    run_id: str, expected: bool
) -> None:
    assert bool(RUN_ID_PATTERN.fullmatch(run_id)) is expected


def test_attempt_log_filename_for_the_architect_omits_the_cycle() -> None:
    name = attempt_log_filename(AgentRole.ARCHITECT, None, 1)

    assert name == "architect-provider-attempt-1.log"


def test_attempt_log_filename_for_the_coder_includes_the_cycle() -> None:
    name = attempt_log_filename(AgentRole.CODER, 1, 1)

    assert name == "coder-cycle-1-provider-attempt-1.log"


def test_attempt_log_filename_for_the_reviewer_distinguishes_provider_attempts() -> (
    None
):
    first = attempt_log_filename(AgentRole.REVIEWER, 1, 1)
    second = attempt_log_filename(AgentRole.REVIEWER, 1, 2)

    assert first == "reviewer-cycle-1-provider-attempt-1.log"
    assert second == "reviewer-cycle-1-provider-attempt-2.log"
    assert first != second


def test_attempt_log_filename_distinguishes_review_cycles() -> None:
    cycle_one = attempt_log_filename(AgentRole.CODER, 1, 1)
    cycle_two = attempt_log_filename(AgentRole.CODER, 2, 1)

    assert cycle_one != cycle_two


def test_attempt_log_filename_rejects_a_cycle_for_the_architect() -> None:
    with pytest.raises(ValueError, match="architect review_cycle must be None"):
        attempt_log_filename(AgentRole.ARCHITECT, 1, 1)


def test_attempt_log_filename_requires_a_cycle_for_the_coder() -> None:
    with pytest.raises(ValueError, match="require a review_cycle"):
        attempt_log_filename(AgentRole.CODER, None, 1)


def test_attempt_log_filename_requires_a_cycle_for_the_reviewer() -> None:
    with pytest.raises(ValueError, match="require a review_cycle"):
        attempt_log_filename(AgentRole.REVIEWER, None, 1)


def test_attempt_log_filename_rejects_a_non_agent_role() -> None:
    with pytest.raises(TypeError, match="role must be AgentRole"):
        attempt_log_filename("architect", None, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("review_cycle", [0, -1])
def test_attempt_log_filename_rejects_a_non_positive_review_cycle(
    review_cycle: int,
) -> None:
    with pytest.raises(ValueError, match="review_cycle must be a positive integer"):
        attempt_log_filename(AgentRole.CODER, review_cycle, 1)


@pytest.mark.parametrize("provider_attempt", [0, -1])
def test_attempt_log_filename_rejects_a_non_positive_provider_attempt(
    provider_attempt: int,
) -> None:
    with pytest.raises(ValueError, match="provider_attempt must be a positive integer"):
        attempt_log_filename(AgentRole.ARCHITECT, None, provider_attempt)

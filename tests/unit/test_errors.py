"""Unit tests for the typed internal error model and its ErrorRecord conversion."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opencode_tools.domain import PipelinePhase, RunOutcome, to_primitive
from opencode_tools.errors import (
    AgentReportedFailureError,
    ConfigError,
    GitSafetyError,
    LoggingError,
    OpenCodeToolsError,
    PreflightError,
    ProcessError,
    ProcessTimeoutError,
    ProtocolError,
    ProviderError,
    ReviewCyclesExhaustedError,
    RunInterruptedError,
    to_error_records,
)

NOW = datetime(2026, 9, 13, 8, 9, 10, 123456, tzinfo=UTC)

ERROR_CLASSES_BY_OUTCOME = {
    RunOutcome.CONFIG_ERROR: ConfigError,
    RunOutcome.PREFLIGHT_ERROR: PreflightError,
    RunOutcome.PROVIDER_ERROR: ProviderError,
    RunOutcome.TIMEOUT: ProcessTimeoutError,
    RunOutcome.PROCESS_ERROR: ProcessError,
    RunOutcome.PROTOCOL_ERROR: ProtocolError,
    RunOutcome.AGENT_REPORTED_FAILURE: AgentReportedFailureError,
    RunOutcome.REVIEW_CYCLES_EXHAUSTED: ReviewCyclesExhaustedError,
    RunOutcome.GIT_SAFETY_ERROR: GitSafetyError,
    RunOutcome.LOGGING_ERROR: LoggingError,
    RunOutcome.INTERRUPTED: RunInterruptedError,
}


def test_every_error_outcome_has_exactly_one_typed_exception() -> None:
    error_outcomes = {
        outcome for outcome in RunOutcome if outcome is not RunOutcome.SUCCEEDED
    }

    assert set(ERROR_CLASSES_BY_OUTCOME) == error_outcomes


@pytest.mark.parametrize(
    ("outcome", "error_type"),
    list(ERROR_CLASSES_BY_OUTCOME.items()),
    ids=lambda value: value.name if isinstance(value, RunOutcome) else value.__name__,
)
def test_each_typed_exception_fixes_its_outcome(
    error_type: type[OpenCodeToolsError],
    outcome: RunOutcome,
) -> None:
    error = error_type("git.branch_drift", "The target branch changed.")

    assert error.outcome is outcome
    assert isinstance(error, OpenCodeToolsError)
    assert isinstance(error, Exception)


def test_typed_exception_rejects_empty_code_and_message() -> None:
    with pytest.raises(ValueError):
        ProcessError("", "Process failed to start.")

    with pytest.raises(ValueError):
        ProcessError("process.spawn_failed", "")


def test_typed_exception_rejects_non_string_code_or_message() -> None:
    with pytest.raises(TypeError):
        ProcessError(1, "Process failed to start.")  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        ProcessError("process.spawn_failed", None)  # type: ignore[arg-type]


def test_typed_exception_rejects_empty_optional_fields() -> None:
    with pytest.raises(ValueError):
        ProcessError(
            "process.spawn_failed",
            "Process failed to start.",
            technical_detail="",
        )

    with pytest.raises(ValueError):
        ProcessError(
            "process.spawn_failed",
            "Process failed to start.",
            related_record="",
        )


def test_typed_exception_rejects_causes_of_the_wrong_type() -> None:
    with pytest.raises(TypeError):
        ProcessError(
            "process.spawn_failed",
            "Process failed to start.",
            causes=(RuntimeError("raw"),),  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError):
        ProcessError(
            "process.spawn_failed",
            "Process failed to start.",
            causes=[ConfigError("config.invalid", "Invalid config.")],  # type: ignore[arg-type]
        )


def test_to_error_records_maps_one_exception_to_one_record() -> None:
    error = GitSafetyError(
        "git.branch_drift",
        "The target branch changed during the run.",
        technical_detail="expected feat/domain, observed main",
        related_record="git-check-2",
    )

    records = to_error_records(
        error,
        phase=PipelinePhase.POSTFLIGHT,
        timestamp=NOW,
        first_sequence=3,
    )

    assert len(records) == 1
    record = records[0]
    assert record.sequence == 3
    assert record.timestamp == NOW
    assert record.phase is PipelinePhase.POSTFLIGHT
    assert record.outcome is RunOutcome.GIT_SAFETY_ERROR
    assert record.code == "git.branch_drift"
    assert record.message == "The target branch changed during the run."
    assert record.technical_detail == "expected feat/domain, observed main"
    assert record.related_record == "git-check-2"


def test_to_error_records_leaves_nullable_fields_unset() -> None:
    error = PreflightError("preflight.tool_missing", "The gh CLI is missing.")

    (record,) = to_error_records(
        error,
        phase=PipelinePhase.PREFLIGHT,
        timestamp=NOW,
        first_sequence=0,
    )

    assert record.related_record is None
    assert record.technical_detail is None


def test_to_error_records_preserves_and_orders_multiple_causes() -> None:
    first_cause = ConfigError(
        "config.invalid_key",
        "review_cycle_limit must be a positive integer.",
        related_record="config[review_cycle_limit]",
    )
    second_cause = ConfigError(
        "config.invalid_key",
        "provider_attempt_limit must be a positive integer.",
        related_record="config[provider_attempt_limit]",
    )
    aggregate = ConfigError(
        "config.invalid",
        "The configuration file has 2 invalid keys.",
        causes=(first_cause, second_cause),
    )

    records = to_error_records(
        aggregate,
        phase=PipelinePhase.PREFLIGHT,
        timestamp=NOW,
        first_sequence=5,
    )

    assert [record.sequence for record in records] == [5, 6, 7]
    assert [record.code for record in records] == [
        "config.invalid_key",
        "config.invalid_key",
        "config.invalid",
    ]
    assert [record.related_record for record in records] == [
        "config[review_cycle_limit]",
        "config[provider_attempt_limit]",
        None,
    ]
    assert all(record.outcome is RunOutcome.CONFIG_ERROR for record in records)


def test_to_error_records_flattens_nested_causes_depth_first() -> None:
    root_cause = ProcessError("process.spawn_failed", "The probe process failed.")
    middle_cause = GitSafetyError(
        "git.probe_indeterminate",
        "The Git safety probe could not complete.",
        causes=(root_cause,),
    )
    top = GitSafetyError(
        "git.checkpoint_failed",
        "The Git safety checkpoint failed.",
        causes=(middle_cause,),
    )

    records = to_error_records(
        top,
        phase=PipelinePhase.POSTFLIGHT,
        timestamp=NOW,
        first_sequence=0,
    )

    assert [record.code for record in records] == [
        "process.spawn_failed",
        "git.probe_indeterminate",
        "git.checkpoint_failed",
    ]
    assert [record.outcome for record in records] == [
        RunOutcome.PROCESS_ERROR,
        RunOutcome.GIT_SAFETY_ERROR,
        RunOutcome.GIT_SAFETY_ERROR,
    ]


def test_to_error_records_rejects_a_non_typed_error() -> None:
    with pytest.raises(TypeError):
        to_error_records(
            RuntimeError("raw"),  # type: ignore[arg-type]
            phase=PipelinePhase.PREFLIGHT,
            timestamp=NOW,
            first_sequence=0,
        )


def test_to_error_records_output_round_trips_through_to_primitive() -> None:
    error = LoggingError(
        "runlog.replace_failed",
        "Failed to persist the run record.",
        technical_detail="os.replace raised OSError(errno=28)",
    )

    (record,) = to_error_records(
        error,
        phase=PipelinePhase.FINALIZATION,
        timestamp=NOW,
        first_sequence=0,
    )
    primitive = to_primitive(record)

    assert isinstance(primitive, dict)
    assert primitive["code"] == "runlog.replace_failed"
    assert primitive["outcome"] == "LOGGING_ERROR"
    assert primitive["phase"] == "FINALIZATION"


def test_to_error_records_never_carries_raw_exception_state() -> None:
    error = ProcessError("process.spawn_failed", "Process failed to start.")

    (record,) = to_error_records(
        error,
        phase=PipelinePhase.PREFLIGHT,
        timestamp=NOW,
        first_sequence=0,
    )

    record_fields = {f.name for f in type(record).__dataclass_fields__.values()}
    assert record_fields == {
        "sequence",
        "timestamp",
        "phase",
        "outcome",
        "code",
        "message",
        "related_record",
        "technical_detail",
    }

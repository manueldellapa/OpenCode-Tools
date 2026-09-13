"""Typed internal exceptions and their deterministic conversion to records.

Each exception carries only pre-sanitized, already action-oriented data --
never a traceback, raw stderr, environment values, or credentials; callers
summarize those before raising. This module knows nothing about retry,
review-cycle, or final-status decisions: it only names a technical cause and
converts it losslessly into one or more `ErrorRecord` instances.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from opencode_tools.domain import ErrorRecord, PipelinePhase, RunOutcome


def _require_non_empty(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_optional_non_empty(value: object, field_name: str) -> None:
    if value is not None:
        _require_non_empty(value, field_name)


def _require_causes(value: object) -> tuple[OpenCodeToolsError, ...]:
    if not isinstance(value, tuple):
        raise TypeError("causes must be a tuple of OpenCodeToolsError")
    for item in value:
        if not isinstance(item, OpenCodeToolsError):
            raise TypeError("causes must contain only OpenCodeToolsError")
    return value


class OpenCodeToolsError(Exception):
    """Base for internal errors mapped one-to-one onto an error `RunOutcome`.

    `causes` preserves concurrent contributing errors in occurrence order
    (earliest first); it is not Python's implicit exception chaining, which
    could otherwise pull an unsanitized raw exception into a record.
    """

    outcome: ClassVar[RunOutcome]

    def __init__(
        self,
        code: str,
        message: str,
        *,
        technical_detail: str | None = None,
        related_record: str | None = None,
        causes: tuple[OpenCodeToolsError, ...] = (),
    ) -> None:
        _require_non_empty(code, "code")
        _require_non_empty(message, "message")
        _require_optional_non_empty(technical_detail, "technical_detail")
        _require_optional_non_empty(related_record, "related_record")
        causes = _require_causes(causes)
        super().__init__(message)
        self.code = code
        self.message = message
        self.technical_detail = technical_detail
        self.related_record = related_record
        self.causes = causes


class ConfigError(OpenCodeToolsError):
    """Invalid or out-of-range configuration, detected before run init."""

    outcome = RunOutcome.CONFIG_ERROR


class PreflightError(OpenCodeToolsError):
    """Tool, agent, auth, repository, lock, or runtime incompatibility."""

    outcome = RunOutcome.PREFLIGHT_ERROR


class ProviderError(OpenCodeToolsError):
    """A trusted provider-classifier signal (rate limit, overload, ...)."""

    outcome = RunOutcome.PROVIDER_ERROR


class ProcessTimeoutError(OpenCodeToolsError):
    """A bounded child process missed its deadline."""

    outcome = RunOutcome.TIMEOUT


class ProcessError(OpenCodeToolsError):
    """A spawn failure or a non-provider non-zero exit."""

    outcome = RunOutcome.PROCESS_ERROR


class ProtocolError(OpenCodeToolsError):
    """An invalid transport, agent identity, marker, or envelope."""

    outcome = RunOutcome.PROTOCOL_ERROR


class AgentReportedFailureError(OpenCodeToolsError):
    """A valid `AGENT_STATUS: FAILED` marker with an explanation."""

    outcome = RunOutcome.AGENT_REPORTED_FAILURE


class ReviewCyclesExhaustedError(OpenCodeToolsError):
    """`CHANGES_REQUIRED` survived through the last allowed review cycle."""

    outcome = RunOutcome.REVIEW_CYCLES_EXHAUSTED


class GitSafetyError(OpenCodeToolsError):
    """Git drift, a read-only-role mutation, or an indeterminate probe."""

    outcome = RunOutcome.GIT_SAFETY_ERROR


class LoggingError(OpenCodeToolsError):
    """An artifact open/write/fsync/replace failure."""

    outcome = RunOutcome.LOGGING_ERROR


class RunInterruptedError(OpenCodeToolsError):
    """A handled SIGINT/SIGTERM cancellation."""

    outcome = RunOutcome.INTERRUPTED


def _flatten(error: OpenCodeToolsError) -> tuple[OpenCodeToolsError, ...]:
    flattened: list[OpenCodeToolsError] = []
    for cause in error.causes:
        flattened.extend(_flatten(cause))
    flattened.append(error)
    return tuple(flattened)


def to_error_records(
    error: OpenCodeToolsError,
    *,
    phase: PipelinePhase,
    timestamp: datetime,
    first_sequence: int,
) -> tuple[ErrorRecord, ...]:
    """Flatten `error` and its causes into ordered, sequenced `ErrorRecord`s.

    Causes are emitted before the error they contributed to, preserving
    occurrence order; `first_sequence` is assigned to the earliest cause and
    each later record increments by one. `phase` and `timestamp` describe the
    single moment of this conversion, not each cause's own origin.
    """

    if not isinstance(error, OpenCodeToolsError):
        raise TypeError("error must be OpenCodeToolsError")

    ordered = _flatten(error)
    return tuple(
        ErrorRecord(
            sequence=first_sequence + index,
            timestamp=timestamp,
            phase=phase,
            outcome=item.outcome,
            code=item.code,
            message=item.message,
            related_record=item.related_record,
            technical_detail=item.technical_detail,
        )
        for index, item in enumerate(ordered)
    )


__all__ = (
    "AgentReportedFailureError",
    "ConfigError",
    "GitSafetyError",
    "LoggingError",
    "OpenCodeToolsError",
    "PreflightError",
    "ProcessError",
    "ProcessTimeoutError",
    "ProtocolError",
    "ProviderError",
    "ReviewCyclesExhaustedError",
    "RunInterruptedError",
    "to_error_records",
)

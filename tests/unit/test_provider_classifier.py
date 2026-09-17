"""Unit tests for the versioned OpenCode `1.17.18` provider classifier
(M07-06, GitHub issue #80).

`classify_provider_signal` reads only two trusted, structurally exact
shapes (System Design SS10.6, FR-028): a `session.error` event's
allowlisted `error.data.code` field, and a top-level `error` event whose
`error.data.message` is itself a serialized JSON payload carrying an
allowlisted numeric `code` / `metadata.error_type` pair (issue #80). It
never reads issue text, assistant/tool/reasoning content, or stderr, so a
lookalike string on any of those channels can never trigger a retry
(AC-031). Most fixtures used here come from the offline M07-01 pack; the
`nested-error-*` fixtures are sanitized reproductions of a genuine
OpenCode 1.17.18 capture (issue #80).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencode_tools.domain import (
    AgentRole,
    GitSafetyStatus,
    PersistenceStatus,
    ProviderDiagnostic,
    ProviderRetryConfig,
    RunOutcome,
)
from opencode_tools.opencode import classify_provider_signal
from opencode_tools.retry import decide_retry

FIXTURES_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "opencode" / "1.17.18"
)
PROVIDER_FIXTURES = FIXTURES_ROOT / "provider"
RUN_FIXTURES = FIXTURES_ROOT / "run"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _provider_text(name: str) -> str:
    return _text(PROVIDER_FIXTURES / name)


# --- every canonical trusted signature is recognized -------------------------


@pytest.mark.parametrize(
    "fixture_name,expected_signature,expected_status,expected_code",
    [
        ("http-429-rate-limit.ndjson", "429", 429, "rate_limit_exceeded"),
        ("http-502-bad-gateway.ndjson", "502", 502, "bad_gateway"),
        (
            "provider-unavailable.ndjson",
            "provider_unavailable",
            503,
            "provider_unavailable",
        ),
        ("overload.ndjson", "overload", None, "overloaded_error"),
        ("rate-limit-textual.ndjson", "rate_limit", None, "rate_limit"),
    ],
)
def test_classify_provider_signal_recognizes_every_canonical_signature(
    fixture_name: str,
    expected_signature: str,
    expected_status: int | None,
    expected_code: str,
) -> None:
    diagnostic = classify_provider_signal(_provider_text(fixture_name))
    assert isinstance(diagnostic, ProviderDiagnostic)
    assert diagnostic.source == "session.error"
    assert diagnostic.signature == expected_signature
    assert diagnostic.retryable is True
    assert diagnostic.status_code == expected_status
    assert diagnostic.code == expected_code


# --- lookalikes on every non-trusted channel never match ---------------------


def test_classify_provider_signal_ignores_a_lookalike_in_assistant_text() -> None:
    diagnostic = classify_provider_signal(
        _provider_text("lookalike-in-assistant-text.ndjson")
    )
    assert diagnostic is None


def test_classify_provider_signal_ignores_a_lookalike_in_tool_output() -> None:
    diagnostic = classify_provider_signal(
        _provider_text("lookalike-in-tool-output.ndjson")
    )
    assert diagnostic is None


def test_classify_provider_signal_ignores_a_lookalike_in_issue_like_text() -> None:
    issue_like = (
        "The upstream API returns 502 Bad Gateway under load; please add a "
        "retry with backoff and handle provider_unavailable gracefully."
    )
    assert classify_provider_signal(issue_like) is None


def test_classify_provider_signal_ignores_a_lookalike_in_stderr_like_text() -> None:
    stderr_like = (
        "Error: 429 Too Many Requests\noverloaded_error\nprovider_unavailable\n"
    )
    assert classify_provider_signal(stderr_like) is None


# --- an uncertain or absent signal is never classified as a provider error ---


@pytest.mark.parametrize(
    "fixture_name",
    [
        "architect-ready-success.ndjson",
        "coder-completed-success.ndjson",
        "reviewer-approved-success.ndjson",
    ],
)
def test_classify_provider_signal_returns_none_for_clean_success_transcripts(
    fixture_name: str,
) -> None:
    assert classify_provider_signal(_text(RUN_FIXTURES / fixture_name)) is None


def test_classify_provider_signal_returns_none_for_an_unrecognized_error_code() -> None:
    text = (
        '{"type": "session.error", "sessionID": "ses_x", '
        '"error": {"name": "ProviderSomethingElseError", '
        '"data": {"code": "unknown_transient_code"}}}'
    )
    assert classify_provider_signal(text) is None


def test_classify_provider_signal_returns_none_for_empty_input() -> None:
    assert classify_provider_signal("") is None


def test_classify_provider_signal_is_lenient_on_malformed_surrounding_lines() -> None:
    # A trusted signal must still be found even when other lines in the same
    # stream are malformed or unrecognized: a transport violation is a
    # concurrent diagnostic, not a reason to miss a genuine trusted signal
    # elsewhere in the same stream (ADR-002).
    text = "\n".join(
        [
            "{not valid json",
            '{"type": "session.debug", "sessionID": "ses_x"}',
            _provider_text("http-429-rate-limit.ndjson").strip(),
        ]
    )
    diagnostic = classify_provider_signal(text)
    assert diagnostic is not None
    assert diagnostic.signature == "429"


# --- concurrent trusted signals follow a deterministic precedence -----------


def test_classify_provider_signal_returns_the_first_signal_in_stream_order() -> None:
    first = _provider_text("http-429-rate-limit.ndjson").strip()
    second = _provider_text("http-502-bad-gateway.ndjson").strip()
    diagnostic = classify_provider_signal(f"{first}\n{second}")
    assert diagnostic is not None
    assert diagnostic.signature == "429"


# --- AC-031: trusted provider boundary and precedence -----------------------


def test_ac_031_trusted_provider_boundary_and_precedence() -> None:
    # A signature present only in a non-trusted channel -- assistant text,
    # tool output, issue-like text, or stderr-like text -- must never trigger
    # a retry, regardless of how closely it mimics a trusted signature.
    assert (
        classify_provider_signal(_provider_text("lookalike-in-assistant-text.ndjson"))
        is None
    )
    assert (
        classify_provider_signal(_provider_text("lookalike-in-tool-output.ndjson"))
        is None
    )

    issue_like = (
        "The upstream API returns 502 Bad Gateway under load; please add a "
        "retry with backoff and handle provider_unavailable gracefully."
    )
    assert classify_provider_signal(issue_like) is None

    stderr_like = (
        "Error: 429 Too Many Requests\noverloaded_error\nprovider_unavailable\n"
    )
    assert classify_provider_signal(stderr_like) is None

    # A trusted provider event -- a `session.error` with an allowlisted
    # `error.data.code` -- does trigger it.
    diagnostic = classify_provider_signal(_provider_text("http-429-rate-limit.ndjson"))
    assert isinstance(diagnostic, ProviderDiagnostic)
    assert diagnostic.source == "session.error"
    assert diagnostic.retryable is True

    # Concurrent trusted signals follow the documented precedence: the first
    # signal in stream order wins.
    first = _provider_text("http-429-rate-limit.ndjson").strip()
    second = _provider_text("http-502-bad-gateway.ndjson").strip()
    precedence_diagnostic = classify_provider_signal(f"{first}\n{second}")
    assert precedence_diagnostic is not None
    assert precedence_diagnostic.signature == "429"


# --- issue #80: top-level `error` event with a nested serialized payload ----


def test_classify_provider_signal_recognizes_the_nested_error_overload_event() -> None:
    """Regression for issue #80: a genuine OpenCode 1.17.18 top-level
    `error` event (session.error's untagged sibling type) whose
    `error.data.message` is a serialized JSON payload carrying code 503 /
    metadata.error_type 'provider_overloaded' must classify as a trusted,
    retryable PROVIDER_ERROR -- not fall through to PROCESS_ERROR with a
    null diagnostic, as it did in v0.1.0."""

    diagnostic = classify_provider_signal(
        _provider_text("nested-error-provider-overloaded.ndjson")
    )
    assert isinstance(diagnostic, ProviderDiagnostic)
    assert diagnostic.source == "error"
    assert diagnostic.signature == "overload"
    assert diagnostic.retryable is True
    assert diagnostic.status_code == 503
    assert diagnostic.code == "provider_overloaded"


def test_classify_provider_signal_ignores_the_nested_error_lookalike_in_tool_output() -> (
    None
):
    """The same code/error_type strings on an ordinary tool-output/
    assistant-text channel -- never inside a top-level `error` event's own
    `error.data.message` field -- must never be classified (AC-031's
    trust-boundary guarantee extended to the new shape)."""

    diagnostic = classify_provider_signal(
        _provider_text("lookalike-nested-error-in-tool-output.ndjson")
    )
    assert diagnostic is None


@pytest.mark.parametrize(
    "event_json",
    [
        # `error.data.message` is not valid JSON at all.
        (
            '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError", '
            '"data": {"message": "{not valid json"}}}'
        ),
        # `error.data.message` is valid JSON, but not an object.
        (
            '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError", '
            '"data": {"message": "503"}}}'
        ),
        # `error.data.message` is missing entirely.
        (
            '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError", '
            '"data": {}}}'
        ),
        # `error.data.message` is already an object, not a serialized string
        # -- the real shape double-encodes it; a bare object is untrusted.
        (
            '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError", '
            '"data": {"message": {"code": 503, '
            '"metadata": {"error_type": "provider_overloaded"}}}}}'
        ),
        # `code` is a string, not the trusted numeric type.
        (
            '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError", '
            '"data": {"message": "{\\"code\\": \\"503\\", \\"metadata\\": '
            '{\\"error_type\\": \\"provider_overloaded\\"}}"}}}'
        ),
        # `metadata` is missing.
        (
            '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError", '
            '"data": {"message": "{\\"code\\": 503}"}}}'
        ),
        # `metadata.error_type` is missing.
        (
            '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError", '
            '"data": {"message": "{\\"code\\": 503, \\"metadata\\": {}}"}}}'
        ),
        # A structurally valid nested payload with an untrusted `code`.
        (
            '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError", '
            '"data": {"message": "{\\"code\\": 500, \\"metadata\\": '
            '{\\"error_type\\": \\"provider_overloaded\\"}}"}}}'
        ),
        # A structurally valid nested payload with an untrusted `error_type`.
        (
            '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError", '
            '"data": {"message": "{\\"code\\": 503, \\"metadata\\": '
            '{\\"error_type\\": \\"some_other_condition\\"}}"}}}'
        ),
        # `error` itself is missing.
        '{"type": "error", "sessionID": "ses_x"}',
        # `error.data` is missing.
        '{"type": "error", "sessionID": "ses_x", "error": {"name": "UnknownError"}}',
    ],
    ids=[
        "malformed-nested-json",
        "nested-json-not-an-object",
        "message-field-missing",
        "message-is-an-object-not-a-serialized-string",
        "code-is-a-string-not-an-int",
        "metadata-missing",
        "error-type-missing",
        "untrusted-code",
        "untrusted-error-type",
        "error-field-missing",
        "data-field-missing",
    ],
)
def test_classify_provider_signal_fails_closed_on_malformed_or_untrusted_nested_error(
    event_json: str,
) -> None:
    """Every malformed or unrecognized variant of the nested-error shape is
    skipped, never guessed (issue #80's fail-closed requirement)."""

    assert classify_provider_signal(event_json) is None


def test_classify_provider_signal_ignores_a_session_error_reusing_the_nested_shape() -> (
    None
):
    """The nested-error allowlist is scoped to the top-level `error` event
    type only: the same trusted (code, error_type) pair carried by a
    `session.error` event -- a different transport shape this classifier
    also trusts, but via its own, independent `error.data.code` field --
    must not be recognized through the nested-error path."""

    text = (
        '{"type": "session.error", "sessionID": "ses_x", '
        '"error": {"name": "UnknownError", "data": {"message": '
        '"{\\"code\\": 503, \\"metadata\\": '
        '{\\"error_type\\": \\"provider_overloaded\\"}}"}}}'
    )
    assert classify_provider_signal(text) is None


def test_classify_provider_signal_orders_session_error_and_nested_error_by_stream_position() -> (
    None
):
    """Precedence between the two trusted shapes is purely stream order,
    like precedence within a single shape (no shape is preferred)."""

    session_error_first = _provider_text("http-429-rate-limit.ndjson").strip()
    nested_error_second = _provider_text(
        "nested-error-provider-overloaded.ndjson"
    ).strip()

    first_wins = classify_provider_signal(
        f"{session_error_first}\n{nested_error_second}"
    )
    assert first_wins is not None
    assert first_wins.signature == "429"

    second_wins = classify_provider_signal(
        f"{nested_error_second}\n{session_error_first}"
    )
    assert second_wins is not None
    assert second_wins.source == "error"
    assert second_wins.signature == "overload"


def test_coder_target_change_still_suppresses_retry_for_the_nested_error_diagnostic() -> (
    None
):
    """Issue #80's preserved retry-safety semantics, proven end to end from
    the real classifier's output (not a synthetic diagnostic): even though
    the nested-error event is now a trusted, retryable PROVIDER_ERROR, a
    coder whose target fingerprint already changed still never gets an
    automatic provider retry (System Design SS11.3/SS12.2)."""

    diagnostic = classify_provider_signal(
        _provider_text("nested-error-provider-overloaded.ndjson")
    )
    assert diagnostic is not None
    assert diagnostic.retryable is True

    config = ProviderRetryConfig(
        max_attempts=3,
        initial_delay_seconds=2,
        multiplier=2.0,
        max_delay_seconds=30,
    )

    decision = decide_retry(
        outcome=RunOutcome.PROVIDER_ERROR,
        provider_diagnostic=diagnostic,
        provider_attempt=1,
        role=AgentRole.CODER,
        target_changed=True,
        termination_confirmed=True,
        git_safety_status=GitSafetyStatus.SAFE,
        persistence_status=PersistenceStatus.OK,
        cancellation_requested=False,
        config=config,
    )
    assert decision.should_retry is False
    assert decision.retry_suppressed_due_to_target_change is True

    # The same diagnostic, for a role other than coder (or without a target
    # change), is still authorized -- the suppression is target-change-and-
    # coder specific, not a blanket denial of this new diagnostic shape.
    unsuppressed = decide_retry(
        outcome=RunOutcome.PROVIDER_ERROR,
        provider_diagnostic=diagnostic,
        provider_attempt=1,
        role=AgentRole.CODER,
        target_changed=False,
        termination_confirmed=True,
        git_safety_status=GitSafetyStatus.SAFE,
        persistence_status=PersistenceStatus.OK,
        cancellation_requested=False,
        config=config,
    )
    assert unsuppressed.should_retry is True
    assert unsuppressed.retry_suppressed_due_to_target_change is False

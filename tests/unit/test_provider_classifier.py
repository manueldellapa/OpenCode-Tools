"""Unit tests for the versioned OpenCode `1.17.18` provider classifier
(M07-06).

`classify_provider_signal` reads only `session.error` events and only
their allowlisted `error.data.code` field (System Design SS10.6, FR-028);
it never reads issue text, assistant/tool/reasoning content, or stderr, so
a lookalike string on any of those channels can never trigger a retry
(AC-031). Every fixture used here comes from the offline M07-01 pack.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencode_tools.domain import ProviderDiagnostic
from opencode_tools.opencode import classify_provider_signal

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

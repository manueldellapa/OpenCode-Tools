"""Unit tests for the `agent-protocol/1` marker, role/status, and body grammar."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from opencode_tools.domain import AgentRole, AgentStatus, ReviewStatus
from opencode_tools.errors import ProtocolError
from opencode_tools.protocol import parse_agent_response

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "protocol"


def _fixture_text(name: str) -> str:
    return (FIXTURES_ROOT / name).read_text(encoding="utf-8")


# --- golden role/status matrix: every canonical combination parses ----------

GOLDEN_CASES: tuple[
    tuple[str, AgentRole, AgentStatus | None, ReviewStatus | None, str], ...
] = (
    (
        "architect-ready.txt",
        AgentRole.ARCHITECT,
        AgentStatus.READY,
        None,
        (
            "Investigated the issue and confirmed the failing scenario in the "
            "retry policy.\nConstraints: keep provider_attempt monotonic and "
            "never retry a non-PROVIDER_ERROR outcome."
        ),
    ),
    (
        "architect-failed.txt",
        AgentRole.ARCHITECT,
        AgentStatus.FAILED,
        None,
        (
            "Unable to resolve the target repository: no Git remote matched "
            "the configured override."
        ),
    ),
    (
        "coder-completed.txt",
        AgentRole.CODER,
        AgentStatus.COMPLETED,
        None,
        (
            "Implemented the retry guard and added regression tests for "
            "target-changed suppression."
        ),
    ),
    (
        "coder-failed.txt",
        AgentRole.CODER,
        AgentStatus.FAILED,
        None,
        (
            "Could not implement the change: the target file was modified in "
            "the working tree during the attempt."
        ),
    ),
    ("reviewer-approved.txt", AgentRole.REVIEWER, None, ReviewStatus.APPROVED, ""),
    (
        "reviewer-changes-required.txt",
        AgentRole.REVIEWER,
        None,
        ReviewStatus.CHANGES_REQUIRED,
        (
            "The retry guard does not suppress a retry when the coder's Git "
            "fingerprint changed mid-attempt."
        ),
    ),
    (
        "reviewer-failed.txt",
        AgentRole.REVIEWER,
        AgentStatus.FAILED,
        None,
        (
            "Could not complete the review: the coder's diff could not be "
            "read from the working tree."
        ),
    ),
)


@pytest.mark.parametrize(
    ("fixture_name", "role", "agent_status", "review_status", "expected_body"),
    GOLDEN_CASES,
    ids=[case[0] for case in GOLDEN_CASES],
)
def test_parses_every_canonical_role_status_combination(
    fixture_name: str,
    role: AgentRole,
    agent_status: AgentStatus | None,
    review_status: ReviewStatus | None,
    expected_body: str,
) -> None:
    result = parse_agent_response(role, _fixture_text(fixture_name))

    assert result.role is role
    assert result.agent_status is agent_status
    assert result.review_status is review_status
    assert result.body == expected_body
    assert result.protocol_version == 1
    # ISSUE_REF_JSON schema validation and locator identity comparison are
    # M06-02 (issue #20); this parser only proves the envelope's position.
    assert result.issue_ref is None


def test_changes_required_is_a_successful_parse_not_an_exception() -> None:
    result = parse_agent_response(
        AgentRole.REVIEWER, _fixture_text("reviewer-changes-required.txt")
    )

    assert result.review_status is ReviewStatus.CHANGES_REQUIRED


# --- body: opaque preservation and the empty/non-empty requirement ---------


def test_prose_that_merely_starts_with_a_reserved_word_stays_opaque_body() -> None:
    # No colon anywhere on these lines: they cannot be attempting any of the
    # four reserved prefixes (all of which are "PREFIX:"), so they must stay
    # ordinary, byte-for-byte opaque body text rather than being routed into
    # the marker grammar and rejected (System Design SS9.2, ADR-002).
    text = (
        "AGENT_STATUSES across the fleet were nominal before this change.\n"
        "FINAL_STATUSES for the retry backoff table were all recorded.\n"
        "AGENT_STATUS: COMPLETED"
    )

    result = parse_agent_response(AgentRole.CODER, text)

    assert result.agent_status is AgentStatus.COMPLETED
    assert result.body == (
        "AGENT_STATUSES across the fleet were nominal before this change.\n"
        "FINAL_STATUSES for the retry backoff table were all recorded."
    )


def test_body_preserves_internal_blank_lines_and_whitespace_byte_for_byte() -> None:
    text = "Line one.\n\n  Indented line two.  \nAGENT_STATUS: FAILED"

    result = parse_agent_response(AgentRole.CODER, text)

    assert result.body == "Line one.\n\n  Indented line two.  "


def test_completed_and_approved_allow_an_empty_body() -> None:
    coder_result = parse_agent_response(AgentRole.CODER, "AGENT_STATUS: COMPLETED")
    reviewer_result = parse_agent_response(
        AgentRole.REVIEWER, "REVIEW_STATUS: APPROVED"
    )

    assert coder_result.body == ""
    assert reviewer_result.body == ""


@pytest.mark.parametrize(
    ("role", "text"),
    [
        (AgentRole.ARCHITECT, 'ISSUE_REF_JSON: {"a": 1}\nAGENT_STATUS: READY'),
        (AgentRole.ARCHITECT, "   \nAGENT_STATUS: FAILED"),
        (AgentRole.CODER, "AGENT_STATUS: FAILED"),
        (AgentRole.REVIEWER, "AGENT_STATUS: FAILED"),
        (AgentRole.REVIEWER, "REVIEW_STATUS: CHANGES_REQUIRED"),
    ],
)
def test_empty_body_is_rejected_when_required(role: AgentRole, text: str) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(role, text)

    assert exc_info.value.code == "protocol.empty_body"


# --- FINAL_STATUS is always reserved to the CLI -----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "FINAL_STATUS: APPROVED",
        "FINAL_STATUS: FAILED\nAGENT_STATUS: COMPLETED",
        "AGENT_STATUS: COMPLETED\nFINAL_STATUS: APPROVED",
        "final_status: approved\nAGENT_STATUS: COMPLETED",
    ],
)
def test_final_status_from_the_agent_is_always_rejected(text: str) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.CODER, text)

    assert exc_info.value.code == "protocol.final_status_reserved"


# --- malformed reserved-prefix lines: casing, spacing, suffix, value -------


@pytest.mark.parametrize(
    "text",
    [
        "agent_status: ready",
        "Agent_Status: READY",
        "AGENT_STATUS:READY",
        "AGENT_STATUS:  READY",
        "AGENT_STATUS: ready",
        "AGENT_STATUS: READY ",
        "AGENT_STATUS: UNKNOWN",
        "AGENT_STATUSX: READY",
        "REVIEW_STATUS: approved",
        "REVIEW_STATUS: READY",
        "REVIEW_STATUS:APPROVED",
        "review_status: approved",
        "REVIEW_STATUS: APPROVED ",
        "ISSUE_REF_JSONX: {}",
        "ISSUE_REF_JSON: ",
        "ISSUE_REF_JSON:{}",
        'issue_ref_json: {"a": 1}',
    ],
)
def test_malformed_reserved_prefix_lines_are_rejected(text: str) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.ARCHITECT, text)

    assert exc_info.value.code == "protocol.malformed_marker"


# --- duplicate and conflicting status markers -------------------------------


def test_duplicate_identical_markers_are_rejected() -> None:
    text = "AGENT_STATUS: COMPLETED\nAGENT_STATUS: COMPLETED"

    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.CODER, text)

    assert exc_info.value.code == "protocol.duplicate_marker"


def test_conflicting_markers_in_the_same_family_are_rejected() -> None:
    text = "AGENT_STATUS: COMPLETED\nAGENT_STATUS: FAILED"

    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.CODER, text)

    assert exc_info.value.code == "protocol.conflicting_marker"


def test_conflicting_markers_across_families_are_rejected() -> None:
    text = "AGENT_STATUS: FAILED\nREVIEW_STATUS: APPROVED"

    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.REVIEWER, text)

    assert exc_info.value.code == "protocol.conflicting_marker"


# --- marker missing or not terminal -----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "   ",
        "Just prose with no marker at all.",
        'ISSUE_REF_JSON: {"a": 1}',
    ],
)
def test_missing_marker_is_rejected(text: str) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.CODER, text)

    assert exc_info.value.code == "protocol.marker_missing"


@pytest.mark.parametrize(
    "text",
    [
        "AGENT_STATUS: COMPLETED\n\n",
        "AGENT_STATUS: COMPLETED\nDone.",
        "AGENT_STATUS: COMPLETED\n ",
    ],
)
def test_marker_not_terminal_is_rejected(text: str) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.CODER, text)

    assert exc_info.value.code == "protocol.marker_not_terminal"


def test_a_marker_line_indented_off_column_zero_is_invisible_to_the_scanner() -> None:
    # Not "not terminal": an indented line is never recognized as a marker at
    # all (System Design SS9.2 requires column zero), so this is indistinguishable
    # from a response that carries no marker whatsoever.
    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.CODER, " AGENT_STATUS: COMPLETED")

    assert exc_info.value.code == "protocol.marker_missing"


def test_single_trailing_newline_after_the_marker_is_still_terminal() -> None:
    result = parse_agent_response(AgentRole.CODER, "AGENT_STATUS: COMPLETED\n")

    assert result.agent_status is AgentStatus.COMPLETED
    assert result.body == ""


# --- the role/status matrix rejects every foreign combination ---------------


@pytest.mark.parametrize(
    ("role", "text"),
    [
        (AgentRole.ARCHITECT, "AGENT_STATUS: COMPLETED"),
        (AgentRole.ARCHITECT, "REVIEW_STATUS: APPROVED"),
        (AgentRole.ARCHITECT, "REVIEW_STATUS: CHANGES_REQUIRED"),
        (AgentRole.CODER, "AGENT_STATUS: READY"),
        (AgentRole.CODER, "REVIEW_STATUS: APPROVED"),
        (AgentRole.CODER, "REVIEW_STATUS: CHANGES_REQUIRED"),
        (AgentRole.REVIEWER, "AGENT_STATUS: READY"),
        (AgentRole.REVIEWER, "AGENT_STATUS: COMPLETED"),
    ],
)
def test_marker_not_allowed_for_role_is_rejected(role: AgentRole, text: str) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(role, text)

    assert exc_info.value.code == "protocol.marker_not_allowed_for_role"


# --- ISSUE_REF_JSON: position and multiplicity only (schema is M06-02) -----


def test_issue_ref_missing_for_architect_ready_is_rejected() -> None:
    text = "Handoff text.\nAGENT_STATUS: READY"

    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.ARCHITECT, text)

    assert exc_info.value.code == "protocol.issue_ref_missing"


def test_issue_ref_present_for_architect_failed_is_rejected() -> None:
    text = 'ISSUE_REF_JSON: {"a": 1}\nAGENT_STATUS: FAILED'

    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.ARCHITECT, text)

    assert exc_info.value.code == "protocol.issue_ref_position"


def test_issue_ref_present_for_a_non_architect_role_is_rejected() -> None:
    text = 'ISSUE_REF_JSON: {"a": 1}\nAGENT_STATUS: COMPLETED'

    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.CODER, text)

    assert exc_info.value.code == "protocol.issue_ref_position"


@pytest.mark.parametrize(
    ("role", "text"),
    [
        (AgentRole.CODER, 'ISSUE_REF_JSON: {"a": 1}\nAGENT_STATUS: READY'),
        (AgentRole.REVIEWER, 'ISSUE_REF_JSON: {"a": 1}\nAGENT_STATUS: READY'),
        (AgentRole.CODER, 'ISSUE_REF_JSON: {"a": 1}\nREVIEW_STATUS: APPROVED'),
    ],
)
def test_role_mismatch_is_rejected_before_envelope_position_is_even_checked(
    role: AgentRole, text: str
) -> None:
    # Proves rule 4 (role match) precedes rule 5 (ISSUE_REF_JSON placement):
    # each of these carries a marker that is invalid for `role` on its own,
    # regardless of the misplaced envelope alongside it.
    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(role, text)

    assert exc_info.value.code == "protocol.marker_not_allowed_for_role"


def test_issue_ref_not_immediately_before_the_marker_is_rejected() -> None:
    text = 'ISSUE_REF_JSON: {"a": 1}\nExtra line.\nAGENT_STATUS: READY'

    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.ARCHITECT, text)

    assert exc_info.value.code == "protocol.issue_ref_position"


def test_issue_ref_appearing_twice_is_rejected() -> None:
    text = 'ISSUE_REF_JSON: {"a": 1}\nISSUE_REF_JSON: {"a": 1}\nAGENT_STATUS: READY'

    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(AgentRole.ARCHITECT, text)

    assert exc_info.value.code == "protocol.issue_ref_duplicate"


# --- SS9.2's 7-step ordered grammar wins deterministically over lower rules -


@pytest.mark.parametrize(
    ("role", "text", "expected_code"),
    [
        # rule 1 (FINAL_STATUS) beats rule 2 (malformed marker).
        (
            AgentRole.CODER,
            "AGENT_STATUS: BOGUS\nFINAL_STATUS: FAILED",
            "protocol.final_status_reserved",
        ),
        # rule 1 (FINAL_STATUS) beats rule 3 (duplicate/conflict).
        (
            AgentRole.CODER,
            "FINAL_STATUS: APPROVED\nAGENT_STATUS: COMPLETED\nAGENT_STATUS: FAILED",
            "protocol.final_status_reserved",
        ),
        # rule 2 (malformed) beats rule 3 (duplicate/conflict).
        (
            AgentRole.CODER,
            "AGENT_STATUS: FAILED\nAGENT_STATUS: ready",
            "protocol.malformed_marker",
        ),
        # rule 4's terminal check beats its own role-match sub-check.
        (
            AgentRole.CODER,
            "AGENT_STATUS: READY\nDone.",
            "protocol.marker_not_terminal",
        ),
        # rule 6 (envelope missing) beats rule 7 (empty body): a bare
        # architect READY marker is missing both, and envelope wins.
        (AgentRole.ARCHITECT, "AGENT_STATUS: READY", "protocol.issue_ref_missing"),
    ],
)
def test_rule_precedence_holds_when_two_violations_coexist(
    role: AgentRole, text: str, expected_code: str
) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_agent_response(role, text)

    assert exc_info.value.code == expected_code


# --- type contract -----------------------------------------------------------


def test_rejects_a_role_that_is_not_an_agent_role_instance() -> None:
    with pytest.raises(TypeError, match="role must be AgentRole"):
        parse_agent_response(cast(AgentRole, "ARCHITECT"), "AGENT_STATUS: READY")


def test_rejects_non_string_text() -> None:
    with pytest.raises(TypeError, match="text must be a string"):
        parse_agent_response(AgentRole.CODER, cast(str, b"AGENT_STATUS: COMPLETED"))

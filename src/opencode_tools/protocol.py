"""Deterministic `agent-protocol/1` marker, role/status, and body grammar.

This module validates only the single terminal assistant text an adapter has
already decoded and newline-normalized (System Design SS9.1-SS9.2); it never
reads NDJSON, tool events, reasoning, or stderr, and it never inspects issue
or repository content beyond the pre-resolved `IssueLocator` it is handed. It
also never classifies a provider signal and never decides a state-machine or
review-cycle outcome -- `CHANGES_REQUIRED` and a valid `AGENT_STATUS: FAILED`
are both a successful *parse* here.

`ISSUE_REF_JSON`'s schema, types, and identity are validated against the
pre-resolved `IssueLocator` (System Design SS9.3): Python never fetches or
semantically checks the GitHub issue itself, only the envelope's own closed
JSON shape and its consistency with what was already resolved before the
architect ran (ADR-002, ADR-007, ADR-010).
"""

from __future__ import annotations

import json

from opencode_tools.domain import (
    AgentRole,
    AgentStatus,
    IssueLocator,
    IssueRef,
    ParsedAgentResponse,
    ReviewStatus,
)
from opencode_tools.errors import ProtocolError

_FINAL_STATUS_FAMILY = "FINAL_STATUS"
_AGENT_STATUS_FAMILY = "AGENT_STATUS"
_REVIEW_STATUS_FAMILY = "REVIEW_STATUS"
_ISSUE_REF_FAMILY = "ISSUE_REF_JSON"
_RESERVED_FAMILIES: tuple[str, ...] = (
    _FINAL_STATUS_FAMILY,
    _AGENT_STATUS_FAMILY,
    _REVIEW_STATUS_FAMILY,
    _ISSUE_REF_FAMILY,
)

_AGENT_STATUS_LINES: dict[str, AgentStatus] = {
    "AGENT_STATUS: READY": AgentStatus.READY,
    "AGENT_STATUS: COMPLETED": AgentStatus.COMPLETED,
    "AGENT_STATUS: FAILED": AgentStatus.FAILED,
}
_REVIEW_STATUS_LINES: dict[str, ReviewStatus] = {
    "REVIEW_STATUS: APPROVED": ReviewStatus.APPROVED,
    "REVIEW_STATUS: CHANGES_REQUIRED": ReviewStatus.CHANGES_REQUIRED,
}
_ISSUE_REF_LINE_PREFIX = "ISSUE_REF_JSON: "

_ROLE_AGENT_STATUSES: dict[AgentRole, frozenset[AgentStatus]] = {
    AgentRole.ARCHITECT: frozenset({AgentStatus.READY, AgentStatus.FAILED}),
    AgentRole.CODER: frozenset({AgentStatus.COMPLETED, AgentStatus.FAILED}),
    AgentRole.REVIEWER: frozenset({AgentStatus.FAILED}),
}

_ISSUE_REF_SCHEMA_KEYS = frozenset(
    {"schema_version", "host", "owner", "repository", "number", "url", "title"}
)
_ISSUE_REF_STRING_KEYS: tuple[str, ...] = (
    "host",
    "owner",
    "repository",
    "url",
    "title",
)
_MAX_ENVELOPE_JSON_LENGTH = 8192
_MAX_ENVELOPE_STRING_LENGTH = 2000


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _require_envelope_string(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise ProtocolError(
            "protocol.issue_ref_invalid_type",
            f"ISSUE_REF_JSON.{key} must be a JSON string.",
        )
    if not value or _contains_control_character(value):
        raise ProtocolError(
            "protocol.issue_ref_invalid_value",
            f"ISSUE_REF_JSON.{key} must be non-empty and free of control characters.",
        )
    if len(value) > _MAX_ENVELOPE_STRING_LENGTH:
        raise ProtocolError(
            "protocol.issue_ref_invalid_value",
            f"ISSUE_REF_JSON.{key} exceeds the defensive length limit.",
        )
    return value


def _validate_issue_ref_envelope(value: str, *, locator: IssueLocator) -> IssueRef:
    """Parse and validate one `ISSUE_REF_JSON` value against `locator`.

    Applies System Design SS9.3's closed v1 schema: exactly seven keys,
    strict JSON integers with booleans explicitly rejected, defensive
    strings, and host/owner/repository/number matching `locator` exactly.
    The canonical HTTPS URL match and non-empty title are then proven by
    constructing `IssueRef` itself. Fetching or semantically checking the
    GitHub issue is out of scope: Python never retrieves it.
    """

    if len(value) > _MAX_ENVELOPE_JSON_LENGTH:
        raise ProtocolError(
            "protocol.issue_ref_invalid_value",
            "ISSUE_REF_JSON exceeds the defensive length limit.",
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise ProtocolError(
            "protocol.issue_ref_invalid_json",
            "ISSUE_REF_JSON is not valid JSON.",
        ) from None

    if type(parsed) is not dict:
        raise ProtocolError(
            "protocol.issue_ref_not_object",
            "ISSUE_REF_JSON must be a single JSON object.",
        )

    keys = frozenset(parsed)
    missing_keys = _ISSUE_REF_SCHEMA_KEYS - keys
    if missing_keys:
        raise ProtocolError(
            "protocol.issue_ref_missing_key",
            f"ISSUE_REF_JSON is missing key(s): {sorted(missing_keys)}.",
        )
    extra_keys = keys - _ISSUE_REF_SCHEMA_KEYS
    if extra_keys:
        raise ProtocolError(
            "protocol.issue_ref_unknown_key",
            f"ISSUE_REF_JSON has unexpected key(s): {sorted(extra_keys)}.",
        )

    schema_version = parsed["schema_version"]
    if type(schema_version) is not int:
        raise ProtocolError(
            "protocol.issue_ref_invalid_type",
            "ISSUE_REF_JSON.schema_version must be a JSON integer.",
        )
    if schema_version != 1:
        raise ProtocolError(
            "protocol.issue_ref_invalid_value",
            "ISSUE_REF_JSON.schema_version must be 1.",
        )

    number = parsed["number"]
    if type(number) is not int:
        raise ProtocolError(
            "protocol.issue_ref_invalid_type",
            "ISSUE_REF_JSON.number must be a JSON integer.",
        )

    strings = {
        key: _require_envelope_string(parsed, key) for key in _ISSUE_REF_STRING_KEYS
    }

    identity = locator.repository_identity
    if (
        strings["host"] != identity.host
        or strings["owner"] != identity.owner
        or strings["repository"] != identity.repository
        or number != locator.number
    ):
        raise ProtocolError(
            "protocol.issue_ref_identity_mismatch",
            "ISSUE_REF_JSON does not match the pre-resolved issue locator.",
        )

    try:
        return IssueRef(
            locator=locator,
            url=strings["url"],
            title=strings["title"],
            schema_version=schema_version,
        )
    except ValueError as error:
        raise ProtocolError("protocol.issue_ref_url_mismatch", str(error)) from None


def _split_logical_lines(text: str) -> tuple[str, ...]:
    """Split `text` into logical lines, consuming one allowed trailing LF.

    A single line terminator after the last content line is permitted and
    does not itself count as another logical line; any further line, even
    empty or whitespace-only, does (System Design SS9.2). Leading/trailing
    spaces on each line are preserved untouched.
    """

    if text == "":
        return ()
    if text.endswith("\n"):
        return tuple(text[:-1].split("\n"))
    return tuple(text.split("\n"))


def _reserved_family(line: str) -> str | None:
    """Return the reserved family `line` attempts, or `None` for plain text.

    Every canonical form is `PREFIX:` followed by a value (System Design
    SS9.2), so a line without any colon at all cannot be attempting one and
    must remain opaque body text -- otherwise ordinary prose that happens to
    start with a reserved word (e.g. "AGENT_STATUSES across the fleet were
    nominal...") would be misrouted into the marker grammar and rejected,
    breaking the byte-for-byte opaque body guarantee (ADR-002). Once a colon
    is present, matching is case-insensitive and prefix-based on purpose: any
    casing, spacing, or suffix deviation from the canonical form is still
    routed through the same family so it is rejected as malformed rather
    than silently treated as opaque body text (ADR-002, ADR-010).
    """

    if ":" not in line:
        return None
    upper = line.upper()
    for family in _RESERVED_FAMILIES:
        if upper.startswith(family):
            return family
    return None


def parse_agent_response(
    role: AgentRole,
    text: str,
    *,
    issue_locator: IssueLocator,
) -> ParsedAgentResponse:
    """Validate one terminal assistant `text` for `role` and parse it.

    Applies the `agent-protocol/1` grammar in the order fixed by System
    Design SS9.2: any `FINAL_STATUS` line is rejected first; then any other
    malformed reserved-prefix line; then duplicate or conflicting status
    markers; then the single terminal marker's position and role match;
    then `ISSUE_REF_JSON` placement, schema, and identity versus
    `issue_locator` (SS9.3); then the non-empty body requirement. Raises
    `ProtocolError` for every violation; never returns a partially valid
    result.
    """

    if type(role) is not AgentRole:
        raise TypeError("role must be AgentRole")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if type(issue_locator) is not IssueLocator:
        raise TypeError("issue_locator must be IssueLocator")

    lines = _split_logical_lines(text)
    reserved_lines = [
        (index, family, line)
        for index, line in enumerate(lines)
        if (family := _reserved_family(line)) is not None
    ]

    if any(family == _FINAL_STATUS_FAMILY for _, family, _ in reserved_lines):
        raise ProtocolError(
            "protocol.final_status_reserved",
            "FINAL_STATUS is reserved for the CLI and must never appear in "
            "agent output.",
        )

    status_markers: list[tuple[int, AgentStatus | None, ReviewStatus | None]] = []
    issue_ref_indexes: list[int] = []

    for index, family, line in reserved_lines:
        if family == _AGENT_STATUS_FAMILY:
            agent_status = _AGENT_STATUS_LINES.get(line)
            if agent_status is None:
                raise ProtocolError(
                    "protocol.malformed_marker",
                    "AGENT_STATUS line does not match a canonical marker.",
                )
            status_markers.append((index, agent_status, None))
        elif family == _REVIEW_STATUS_FAMILY:
            review_status = _REVIEW_STATUS_LINES.get(line)
            if review_status is None:
                raise ProtocolError(
                    "protocol.malformed_marker",
                    "REVIEW_STATUS line does not match a canonical marker.",
                )
            status_markers.append((index, None, review_status))
        else:
            value = line[len(_ISSUE_REF_LINE_PREFIX) :]
            if not line.startswith(_ISSUE_REF_LINE_PREFIX) or not value:
                raise ProtocolError(
                    "protocol.malformed_marker",
                    "ISSUE_REF_JSON line does not match the canonical prefix.",
                )
            issue_ref_indexes.append(index)

    if not status_markers:
        raise ProtocolError(
            "protocol.marker_missing",
            "No terminal AGENT_STATUS or REVIEW_STATUS marker was found.",
        )
    if len(status_markers) > 1:
        distinct = {(agent, review) for _, agent, review in status_markers}
        code = (
            "protocol.duplicate_marker"
            if len(distinct) == 1
            else "protocol.conflicting_marker"
        )
        raise ProtocolError(
            code,
            "More than one status marker line was found in the response.",
        )

    marker_index, agent_status, review_status = status_markers[0]
    if marker_index != len(lines) - 1:
        raise ProtocolError(
            "protocol.marker_not_terminal",
            "The status marker must be the final logical line.",
        )

    if agent_status is not None:
        if agent_status not in _ROLE_AGENT_STATUSES[role]:
            raise ProtocolError(
                "protocol.marker_not_allowed_for_role",
                f"{role.value} may not report AGENT_STATUS: {agent_status.value}.",
            )
    elif role is not AgentRole.REVIEWER:
        raise ProtocolError(
            "protocol.marker_not_allowed_for_role",
            f"{role.value} may not report REVIEW_STATUS.",
        )

    is_architect_ready = (
        role is AgentRole.ARCHITECT and agent_status is AgentStatus.READY
    )

    if len(issue_ref_indexes) > 1:
        raise ProtocolError(
            "protocol.issue_ref_duplicate",
            "ISSUE_REF_JSON may appear at most once.",
        )
    issue_ref: IssueRef | None = None
    if issue_ref_indexes:
        issue_ref_index = issue_ref_indexes[0]
        if not is_architect_ready or issue_ref_index != marker_index - 1:
            raise ProtocolError(
                "protocol.issue_ref_position",
                "ISSUE_REF_JSON must be the line immediately before an "
                "architect READY marker.",
            )
        body_end = issue_ref_index
        envelope_value = lines[issue_ref_index][len(_ISSUE_REF_LINE_PREFIX) :]
        issue_ref = _validate_issue_ref_envelope(envelope_value, locator=issue_locator)
    elif is_architect_ready:
        raise ProtocolError(
            "protocol.issue_ref_missing",
            "Architect READY requires an ISSUE_REF_JSON line immediately "
            "before the marker.",
        )
    else:
        body_end = marker_index

    body = "\n".join(lines[:body_end])
    requires_body = (
        is_architect_ready
        or agent_status is AgentStatus.FAILED
        or review_status is ReviewStatus.CHANGES_REQUIRED
    )
    if requires_body and not body.strip():
        raise ProtocolError(
            "protocol.empty_body",
            "This role and status combination requires a non-empty body.",
        )

    return ParsedAgentResponse(
        role=role,
        body=body,
        agent_status=agent_status,
        review_status=review_status,
        issue_ref=issue_ref,
    )


__all__ = ("parse_agent_response",)

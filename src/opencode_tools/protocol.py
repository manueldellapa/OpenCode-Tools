"""Deterministic `agent-protocol/1` marker, role/status, and body grammar.

This module validates only the single terminal assistant text an adapter has
already decoded and newline-normalized (System Design SS9.1-SS9.2); it never
reads NDJSON, tool events, reasoning, or stderr, and it never inspects issue
or repository content. It also never classifies a provider signal and never
decides a state-machine or review-cycle outcome -- `CHANGES_REQUIRED` and a
valid `AGENT_STATUS: FAILED` are both a successful *parse* here.

`ISSUE_REF_JSON` v1 schema validation and identity comparison against a
pre-resolved `IssueLocator` (System Design SS9.3) are a later milestone; this
module only recognizes the envelope line by its reserved prefix and enforces
its required position, so `ParsedAgentResponse.issue_ref` is always `None`
here (ADR-002, ADR-010).
"""

from __future__ import annotations

from opencode_tools.domain import (
    AgentRole,
    AgentStatus,
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


def parse_agent_response(role: AgentRole, text: str) -> ParsedAgentResponse:
    """Validate one terminal assistant `text` for `role` and parse it.

    Applies the `agent-protocol/1` grammar in the order fixed by System
    Design SS9.2: any `FINAL_STATUS` line is rejected first; then any other
    malformed reserved-prefix line; then duplicate or conflicting status
    markers; then the single terminal marker's position and role match;
    then `ISSUE_REF_JSON` placement; then the non-empty body requirement.
    Raises `ProtocolError` for every violation; never returns a partially
    valid result.
    """

    if type(role) is not AgentRole:
        raise TypeError("role must be AgentRole")
    if not isinstance(text, str):
        raise TypeError("text must be a string")

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
    if issue_ref_indexes:
        issue_ref_index = issue_ref_indexes[0]
        if not is_architect_ready or issue_ref_index != marker_index - 1:
            raise ProtocolError(
                "protocol.issue_ref_position",
                "ISSUE_REF_JSON must be the line immediately before an "
                "architect READY marker.",
            )
        body_end = issue_ref_index
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
    )


__all__ = ("parse_agent_response",)

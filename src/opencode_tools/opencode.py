"""OpenCode `1.17.18` exact-version adapter (System Design SS10, ADR-005).

This module is the only place that knows OpenCode's concrete CLI surface,
NDJSON transport, and JSON utility output shapes for the exact candidate
version `1.17.18`. It never trusts approximate signals -- exit code alone,
`--agent`, or a semver range -- because the upstream CLI can silently fall
back to a default agent; every fact this module asserts is proven against
the offline fixture pack in `tests/fixtures/opencode/1.17.18/` (M07-01) and,
eventually, the live M15-03 qualification smoke.

Everything here is candidate/offline evidence: nothing in this module ever
marks `1.17.18` as a *supported* runtime version. That declaration is
exclusively the M15-03 qualification gate's to make, after this adapter,
its fixtures, and the live smoke all pass.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from opencode_tools.domain import (
    AgentRole,
    ProcessResult,
    ProcessSpec,
    RunOutcome,
    Workspace,
)
from opencode_tools.errors import OpenCodeToolsError, PreflightError, ProtocolError
from opencode_tools.ports import LogChannel, ProcessRunner
from opencode_tools.process import sanitize_command

CANDIDATE_OPENCODE_VERSION = "1.17.18"

_ENVIRONMENT_OVERRIDES: dict[str, str] = {
    "OPENCODE_AUTO_SHARE": "false",
    "OPENCODE_DISABLE_AUTOUPDATE": "true",
}

_PRIMARY_ROLES: tuple[AgentRole, ...] = (
    AgentRole.ARCHITECT,
    AgentRole.CODER,
    AgentRole.REVIEWER,
)
_ROLE_TOKENS: dict[AgentRole, str] = {
    AgentRole.ARCHITECT: "architect",
    AgentRole.CODER: "coder",
    AgentRole.REVIEWER: "reviewer",
}
_REQUIRED_RUN_HELP_TOKENS: tuple[str, ...] = ("--agent", "--format json", "--dir")

# Flags this adapter must never pass to `opencode run` (System Design
# SS10.3): auto/share/model overrides and every form of session
# continuation. Each invocation is its own independent OpenCode session.
FORBIDDEN_RUN_FLAGS: tuple[str, ...] = (
    "--auto",
    "--share",
    "--model",
    "--continue",
    "--session",
    "--fork",
    "--attach",
)

# The reviewed, exact-match permission baseline for each primary role: only
# the coder may edit or run bash against the target; architect and reviewer
# stay read-only. Anything else -- including a more permissive "ask" level a
# non-interactive run could never answer -- fails closed rather than being
# ranked on a permissiveness scale (ADR-005, ADR-010).
_PERMISSION_BASELINE: dict[AgentRole, dict[str, str]] = {
    AgentRole.ARCHITECT: {"edit": "deny", "bash": "deny", "webfetch": "deny"},
    AgentRole.CODER: {"edit": "allow", "bash": "allow", "webfetch": "deny"},
    AgentRole.REVIEWER: {"edit": "deny", "bash": "deny", "webfetch": "deny"},
}

# Versioned defensive buffer for preflight utility output (System Design
# SS10.1's "buffer massimo versionato"); a call that exceeds this never
# raises OSError -- it is a content-size fact for the caller, not an I/O
# fault -- it just stops the affected channel from growing further.
_UTILITY_OUTPUT_LIMIT_BYTES = 1_048_576

# `opencode run` can legitimately produce much more output than a utility
# call (agent conversation, tool output), so it gets its own, larger
# versioned byte budget (System Design SS10.1).
RUN_OUTPUT_LIMIT_BYTES = 8 * 1_048_576

# Independent of the overall byte budget: bounds the number of NDJSON lines
# this adapter will ever parse for one run, so a pathological number of
# tiny lines cannot force unbounded parse work even while staying under the
# byte budget (System Design SS10.5's "limiti dimensionali").
MAX_NDJSON_LINES = 100_000

# The transport-level event/part shapes this exact-version adapter
# recognizes (System Design SS10.5). Anything else -- including a real
# OpenCode event type this adapter simply does not know about yet -- fails
# closed rather than being ignored, per ADR-005's no-best-effort policy.
_ALLOWED_TRANSPORT_EVENT_TYPES = frozenset({"message.part.updated", "session.error"})
_ALLOWED_TRANSPORT_PART_TYPES = frozenset({"text", "reasoning", "tool"})


class _BoundedCapturingSink:
    """An in-memory `AttemptLogSink` that bounds and exposes captured bytes.

    OpenCode utility output is version-sensitive content this adapter must
    parse, but System Design SS10.1/SS18.3 forbid persisting it raw: nothing
    here ever touches disk. A caller that also wants a durable attempt log
    wires a second, real sink at a later milestone; this one exists purely
    so the adapter itself can read what the child printed.
    """

    def __init__(self, *, path: Path, max_bytes: int) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._buffers: dict[str, bytearray] = {}
        self._overflowed: set[str] = set()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        del timestamp
        if channel in self._overflowed:
            return
        buffer = self._buffers.setdefault(channel, bytearray())
        if len(buffer) + len(payload) > self._max_bytes:
            self._overflowed.add(channel)
            return
        buffer.extend(payload)

    def close(self) -> None:
        return None

    def bytes_for(self, channel: LogChannel) -> bytes:
        return bytes(self._buffers.get(channel, b""))

    def overflowed(self, channel: LogChannel) -> bool:
        return channel in self._overflowed


@dataclass(frozen=True, slots=True)
class ControlPlaneEvidence:
    """Immutable proof of one exact-version preflight pass (ADR-005).

    `control_plane_digest` is the canonical digest `recheck_control_plane`
    must still be able to reproduce immediately before every later
    `opencode run` in the same invocation.
    """

    version: str
    executable: Path
    control_plane_digest: str

    def __post_init__(self) -> None:
        if self.version != CANDIDATE_OPENCODE_VERSION:
            raise ValueError("version must be the exact candidate version")
        if not self.executable.is_absolute():
            raise ValueError("executable must be an absolute path")
        if not self.control_plane_digest:
            raise ValueError("control_plane_digest must not be empty")


@dataclass(frozen=True, slots=True)
class TransportResult:
    """The single decoded terminal assistant text and its session (SS10.5).

    `protocol.py` applies the `agent-protocol/1` grammar only to
    `terminal_text`; it never sees the NDJSON this was decoded from.
    """

    session_id: str
    terminal_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(self.terminal_text, str):
            raise TypeError("terminal_text must be a string")


_EXPORT_METHOD = "sanitized_session_export"


@dataclass(frozen=True, slots=True)
class AgentIdentityEvidence:
    """Proof that one invocation's export matched the requested role (SS10.5).

    Only `requested_agent`, `verified_agent`, `method`, `version`, and
    `digest` are ever retained; the raw export JSON and any model/provider
    ID are not (ADR-005, System Design SS18.3). Because this type can only
    be constructed by `verify_agent_identity` after a successful match,
    `requested_agent == verified_agent` is a class invariant, not something
    a caller needs to re-check.
    """

    requested_agent: str
    verified_agent: str
    method: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        for field_name in (
            "requested_agent",
            "verified_agent",
            "method",
            "version",
            "digest",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.requested_agent != self.verified_agent:
            raise ValueError(
                "requested_agent and verified_agent must match a proven identity"
            )


def resolve_executable() -> Path:
    """Resolve the OpenCode executable once via `PATH` (ADR-005, ADR-009).

    Never tries an alias or an automatic download; a missing executable
    fails closed immediately with the phase and action the operator needs.
    """

    found = shutil.which("opencode")
    if found is None:
        raise PreflightError(
            "opencode.executable_not_found",
            "The 'opencode' executable was not found on PATH.",
        )
    return Path(found).resolve()


def check_no_forbidden_flags(argv: tuple[str, ...]) -> None:
    """Assert `argv` contains none of `FORBIDDEN_RUN_FLAGS`.

    This is a self-verification of adapter-internal command construction,
    not a fact about the user's environment: unlike `PreflightError`, its
    trigger would be a bug in this module's own argv construction, never an
    OpenCode incompatibility, so it raises the plain `AssertionError` a
    genuinely impossible internal invariant deserves.
    """

    forbidden_present = tuple(flag for flag in FORBIDDEN_RUN_FLAGS if flag in argv)
    if forbidden_present:
        raise AssertionError(
            f"opencode.py constructed a forbidden flag: {forbidden_present}"
        )


def build_run_spec(
    executable: Path,
    role: AgentRole,
    prompt: str,
    workspace: Workspace,
    *,
    timeout_seconds: float,
    termination_grace_seconds: float,
) -> ProcessSpec:
    """Build the exact `opencode run` invocation for `role` (System Design
    SS10.3).

    Produces exactly `<executable> run --agent <role> --format json --dir
    <workspace>` with one absolute executable path and no other flags.
    `prompt` reaches the child only through stdin, so it can never appear in
    argv, shell history, or a process listing; `cwd` is also set to
    `workspace.root`, duplicating `--dir` deliberately so the invocation
    never depends on the caller's own cwd. Every call therefore starts an
    independent OpenCode session -- there is no `--continue`/`--session`
    that could attach it to a prior one.
    """

    argv = (
        str(executable),
        "run",
        "--agent",
        _ROLE_TOKENS[role],
        "--format",
        "json",
        "--dir",
        str(workspace.root),
    )
    check_no_forbidden_flags(argv)

    return ProcessSpec(
        argv=argv,
        cwd=workspace.root,
        stdin=prompt,
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        environment_overrides=_ENVIRONMENT_OVERRIDES,
    )


def redact_command_for_display(spec: ProcessSpec) -> tuple[str, ...]:
    """Return `spec.argv`, sanitized and with stdin marked, never shown.

    The prompt never reaches argv -- it travels only on stdin -- but a
    persisted command string that showed nothing for it would be
    indistinguishable from a call with no stdin at all. Appending
    `<PROMPT_REDACTED>` only when `spec.stdin` is non-empty keeps that
    distinction explicit without ever exposing what was actually sent
    (System Design SS18.3).
    """

    argv = sanitize_command(spec.argv)
    if spec.stdin:
        return (*argv, "<PROMPT_REDACTED>")
    return argv


def _strict_utf8(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _split_ndjson_lines(text: str) -> tuple[str, ...]:
    """Normalize CRLF/CR to LF only, then split into logical lines.

    Mirrors `protocol.py`'s own `_split_logical_lines` newline handling, but
    independently: ADR-002 keeps the transport and the application-protocol
    grammar from sharing an implementation, not just a responsibility.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized == "":
        return ()
    if normalized.endswith("\n"):
        return tuple(normalized[:-1].split("\n"))
    return tuple(normalized.split("\n"))


def decode_run_transport(text: str) -> TransportResult:
    """Decode already UTF-8-decoded `opencode run --format json` stdout.

    Applies, in order (System Design SS9.2/SS10.5, ADR-002/ADR-005): CRLF/CR
    normalization only; bounded NDJSON line splitting; per-line JSON
    validity and an allowed event-type/part-type/session-ID check; grouping
    completed `text` parts by `messageID` (last write for a given ID wins),
    with `tool` and `reasoning` parts excluded entirely from the result; and
    a requirement that exactly one completed group exists once the stream
    ends. Every violation is `ProtocolError`; this never searches for a
    marker itself -- that is `protocol.py`'s job on the single string this
    function returns.
    """

    lines = _split_ndjson_lines(text)
    if len(lines) > MAX_NDJSON_LINES:
        raise ProtocolError(
            "opencode.transport_output_too_large",
            "opencode run produced more NDJSON lines than the defensive limit.",
        )

    session_id: str | None = None
    completed_text_by_message: dict[str, str] = {}
    completed_order: list[str] = []

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise ProtocolError(
                "opencode.transport_invalid_json",
                "opencode run produced a line that is not valid JSON.",
            ) from None
        if not isinstance(event, dict):
            raise ProtocolError(
                "opencode.transport_invalid_event",
                "An NDJSON line is not a JSON object.",
            )

        event_type = event.get("type")
        if event_type not in _ALLOWED_TRANSPORT_EVENT_TYPES:
            raise ProtocolError(
                "opencode.transport_unknown_event_type",
                f"opencode run produced an unrecognized event type: {event_type!r}.",
            )

        line_session_id = event.get("sessionID")
        if not isinstance(line_session_id, str) or not line_session_id:
            raise ProtocolError(
                "opencode.transport_invalid_event",
                "An NDJSON line is missing a non-empty sessionID.",
            )
        if session_id is None:
            session_id = line_session_id
        elif line_session_id != session_id:
            raise ProtocolError(
                "opencode.transport_session_id_mismatch",
                "opencode run produced more than one distinct sessionID.",
            )

        if event_type != "message.part.updated":
            continue

        part = event.get("part")
        if not isinstance(part, dict):
            raise ProtocolError(
                "opencode.transport_invalid_event",
                "A message.part.updated event has no part object.",
            )
        part_type = part.get("type")
        if part_type not in _ALLOWED_TRANSPORT_PART_TYPES:
            raise ProtocolError(
                "opencode.transport_unknown_event_type",
                f"opencode run produced an unrecognized part type: {part_type!r}.",
            )
        if part_type != "text":
            continue

        message_id = part.get("messageID")
        if not isinstance(message_id, str) or not message_id:
            raise ProtocolError(
                "opencode.transport_invalid_event",
                "A text part is missing a non-empty messageID.",
            )
        text_value = part.get("text")
        if not isinstance(text_value, str):
            raise ProtocolError(
                "opencode.transport_invalid_event",
                "A text part is missing its text string.",
            )

        time_info = part.get("time")
        is_complete = isinstance(time_info, dict) and "end" in time_info
        if is_complete:
            if message_id not in completed_text_by_message:
                completed_order.append(message_id)
            completed_text_by_message[message_id] = text_value

    if not completed_order:
        raise ProtocolError(
            "opencode.transport_no_terminal_text",
            "opencode run produced no completed terminal assistant text.",
        )
    if len(completed_order) > 1:
        raise ProtocolError(
            "opencode.transport_multiple_terminal_candidates",
            "opencode run produced more than one candidate terminal assistant text.",
        )

    assert session_id is not None  # guaranteed once completed_order is non-empty
    return TransportResult(
        session_id=session_id,
        terminal_text=completed_text_by_message[completed_order[0]],
    )


def decode_run_output(stdout: bytes, *, overflowed: bool) -> TransportResult:
    """Decode raw `opencode run` stdout bytes into one terminal assistant text.

    Thin wrapper around `decode_run_transport` that first rejects output
    that already overflowed the versioned size limit (`overflowed`, from
    `sink.overflowed("stdout")` against `RUN_OUTPUT_LIMIT_BYTES`) and output
    that is not strict UTF-8, both `ProtocolError` (ADR-002, ADR-005).
    """

    if overflowed:
        raise ProtocolError(
            "opencode.transport_output_too_large",
            "opencode run stdout exceeded the defensive size limit.",
        )
    text = _strict_utf8(stdout)
    if text is None:
        raise ProtocolError(
            "opencode.transport_invalid_utf8",
            "opencode run stdout was not valid UTF-8.",
        )
    return decode_run_transport(text)


def open_run_capture_sink(log_name: str) -> _BoundedCapturingSink:
    """Open the bounded stdout/stderr capture used for one `opencode run`."""

    return _BoundedCapturingSink(path=Path(log_name), max_bytes=RUN_OUTPUT_LIMIT_BYTES)


def _require_process_succeeded(
    result: ProcessResult,
    *,
    error_cls: type[OpenCodeToolsError],
    code: str,
    message: str,
) -> None:
    if result.outcome is not RunOutcome.SUCCEEDED:
        raise error_cls(
            code,
            message,
            technical_detail=(
                f"outcome={result.outcome.value} return_code={result.return_code}"
            ),
        )


def _decode_or_raise(
    sink: _BoundedCapturingSink, *, error_cls: type[OpenCodeToolsError], code: str
) -> str:
    if sink.overflowed("stdout"):
        raise error_cls(
            code, "OpenCode utility output exceeded the defensive size limit."
        )
    text = _strict_utf8(sink.bytes_for("stdout"))
    if text is None:
        raise error_cls(code, "OpenCode utility output was not valid UTF-8.")
    return text


def _parse_json_object(
    text: str, *, error_cls: type[OpenCodeToolsError], code: str, what: str
) -> dict[str, object]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise error_cls(code, f"{what} was not valid JSON.") from None
    if not isinstance(parsed, dict):
        raise error_cls(code, f"{what} must be a JSON object.")
    return cast(dict[str, object], parsed)


def _run_utility(
    process_runner: ProcessRunner,
    executable: Path,
    argv_tail: tuple[str, ...],
    *,
    log_name: str,
    cwd: Path,
    timeout_seconds: float,
    termination_grace_seconds: float,
) -> tuple[ProcessResult, _BoundedCapturingSink]:
    spec = ProcessSpec(
        argv=(str(executable), *argv_tail),
        cwd=cwd,
        stdin=None,
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        environment_overrides=_ENVIRONMENT_OVERRIDES,
    )
    sink = _BoundedCapturingSink(
        path=Path(log_name), max_bytes=_UTILITY_OUTPUT_LIMIT_BYTES
    )
    result = process_runner.run(spec, sink=sink)
    return result, sink


def check_version(raw_output: str) -> str:
    """Match `raw_output` against the exact candidate version (ADR-005).

    Only an exact match, after stripping surrounding whitespace, selects the
    `1.17.18` adapter; an unknown version, a semver range, or extra banner
    text all fail closed. There is no fallback.
    """

    normalized = raw_output.strip()
    if normalized != CANDIDATE_OPENCODE_VERSION:
        raise PreflightError(
            "opencode.version_mismatch",
            "OpenCode reported a version other than the exact candidate "
            f"{CANDIDATE_OPENCODE_VERSION}.",
            technical_detail=f"reported={normalized!r}",
        )
    return normalized


def check_run_help_capability(raw_output: str) -> None:
    """Verify `run --help` advertises every capability this adapter needs."""

    missing = tuple(
        token for token in _REQUIRED_RUN_HELP_TOKENS if token not in raw_output
    )
    if missing:
        raise PreflightError(
            "opencode.capability_missing",
            f"opencode run --help is missing expected capabilities: {missing}.",
        )


def check_debug_config(config: dict[str, object]) -> None:
    """Reject automatic sharing in `debug config`'s effective policy."""

    if config.get("share") == "auto" or config.get("autoshare") is True:
        raise PreflightError(
            "opencode.debug_config_rejected",
            "opencode debug config reports automatic sharing enabled.",
        )


def check_debug_agent(role: AgentRole, agent: dict[str, object]) -> None:
    """Reject a missing/fallback, non-primary, ask/task-enabled, or
    over-permissive agent.

    The identity check comes first and on its own: OpenCode `1.17.18` can
    silently fall back to a default agent when the requested one is
    unavailable (ADR-005 SS10.4), so a `debug agent <role>` response that
    does not itself claim to be `role` -- whether because the role is not
    defined at all or because the CLI substituted a different one -- must
    fail closed before any policy field is even inspected.
    """

    if agent.get("name") != _ROLE_TOKENS[role]:
        raise PreflightError(
            "opencode.debug_agent_identity_mismatch",
            f"opencode debug agent {_ROLE_TOKENS[role]} did not identify "
            "itself as the requested role; it is missing or the CLI fell "
            "back to a different agent.",
        )
    if agent.get("mode") != "primary":
        raise PreflightError(
            "opencode.debug_agent_rejected",
            f"opencode debug agent {_ROLE_TOKENS[role]} is not mode primary.",
        )
    tools = agent.get("tools")
    if not isinstance(tools, dict):
        raise PreflightError(
            "opencode.debug_agent_invalid",
            f"opencode debug agent {_ROLE_TOKENS[role]} has no tools object.",
        )
    if tools.get("ask") is not False or tools.get("task") is not False:
        raise PreflightError(
            "opencode.debug_agent_rejected",
            f"opencode debug agent {_ROLE_TOKENS[role]} enables ask or task.",
        )
    permission = agent.get("permission")
    if not isinstance(permission, dict):
        raise PreflightError(
            "opencode.debug_agent_invalid",
            f"opencode debug agent {_ROLE_TOKENS[role]} has no permission object.",
        )
    expected = _PERMISSION_BASELINE[role]
    observed = {key: permission.get(key) for key in expected}
    if observed != expected:
        raise PreflightError(
            "opencode.debug_agent_rejected",
            f"opencode debug agent {_ROLE_TOKENS[role]} permission matrix "
            "does not match the reviewed baseline.",
        )


def compute_control_plane_digest(
    *,
    config: dict[str, object],
    agents: dict[AgentRole, dict[str, object]],
) -> str:
    """Compute the canonical SHA-256 digest over the control-plane evidence.

    Hashing the *parsed* structures with sorted keys and compact separators,
    rather than raw bytes, means a harmless re-serialization difference
    between two `debug` calls -- key order, whitespace -- never registers as
    drift; only a genuine change to the effective policy does (ADR-005,
    System Design SS18.2).
    """

    canonical = {
        "config": config,
        "agents": {role.value: agents[role] for role in _PRIMARY_ROLES},
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _fetch_raw_control_plane(
    process_runner: ProcessRunner,
    executable: Path,
    *,
    workspace: Workspace,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> tuple[dict[str, object], dict[AgentRole, dict[str, object]]]:
    """Fetch and JSON-parse `debug config` and the three `debug agent` calls.

    Parses structure only; it deliberately does not apply ADR-005's policy
    rules (share/mode/tools/permission) so `recheck_control_plane` can reuse
    it for a pure digest comparison without re-running full validation.
    """

    config_result, config_sink = _run_utility(
        process_runner,
        executable,
        ("debug", "config"),
        log_name="preflight-debug-config.log",
        cwd=workspace.root,
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    _require_process_succeeded(
        config_result,
        error_cls=PreflightError,
        code="opencode.debug_config_call_failed",
        message="opencode debug config did not complete successfully.",
    )
    config_text = _decode_or_raise(
        config_sink,
        error_cls=PreflightError,
        code="opencode.debug_config_call_failed",
    )
    config = _parse_json_object(
        config_text,
        error_cls=PreflightError,
        code="opencode.debug_config_invalid",
        what="opencode debug config",
    )

    agents: dict[AgentRole, dict[str, object]] = {}
    for role in _PRIMARY_ROLES:
        token = _ROLE_TOKENS[role]
        agent_result, agent_sink = _run_utility(
            process_runner,
            executable,
            ("debug", "agent", token),
            log_name=f"preflight-debug-agent-{token}.log",
            cwd=workspace.root,
            timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
        )
        _require_process_succeeded(
            agent_result,
            error_cls=PreflightError,
            code="opencode.debug_agent_call_failed",
            message=f"opencode debug agent {token} did not complete successfully.",
        )
        agent_text = _decode_or_raise(
            agent_sink,
            error_cls=PreflightError,
            code="opencode.debug_agent_call_failed",
        )
        agents[role] = _parse_json_object(
            agent_text,
            error_cls=PreflightError,
            code="opencode.debug_agent_invalid",
            what=f"opencode debug agent {token}",
        )

    return config, agents


def run_preflight(
    process_runner: ProcessRunner,
    *,
    executable: Path,
    workspace: Workspace,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> ControlPlaneEvidence:
    """Run the full exact-version capability/control-plane preflight sequence.

    Executes, in ADR-005 SS10.4's order: a single `--version` call matched
    exactly against `CANDIDATE_OPENCODE_VERSION`; `run --help` capability
    verification; `debug config`; and `debug agent <role>` for all three
    primary roles, each individually validated. The executable itself is
    resolved once by the caller (`shutil.which`, System Design SS10.4) and
    passed in already absolute.
    """

    version_result, version_sink = _run_utility(
        process_runner,
        executable,
        ("--version",),
        log_name="preflight-version.log",
        cwd=workspace.root,
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    _require_process_succeeded(
        version_result,
        error_cls=PreflightError,
        code="opencode.version_call_failed",
        message="opencode --version did not complete successfully.",
    )
    version_text = _decode_or_raise(
        version_sink, error_cls=PreflightError, code="opencode.version_call_failed"
    )
    version = check_version(version_text)

    help_result, help_sink = _run_utility(
        process_runner,
        executable,
        ("run", "--help"),
        log_name="preflight-run-help.log",
        cwd=workspace.root,
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    _require_process_succeeded(
        help_result,
        error_cls=PreflightError,
        code="opencode.capability_call_failed",
        message="opencode run --help did not complete successfully.",
    )
    help_text = _decode_or_raise(
        help_sink, error_cls=PreflightError, code="opencode.capability_call_failed"
    )
    check_run_help_capability(help_text)

    config, agents = _fetch_raw_control_plane(
        process_runner,
        executable,
        workspace=workspace,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    check_debug_config(config)
    for role in _PRIMARY_ROLES:
        check_debug_agent(role, agents[role])
    digest = compute_control_plane_digest(config=config, agents=agents)

    return ControlPlaneEvidence(
        version=version, executable=executable, control_plane_digest=digest
    )


def recheck_control_plane(
    process_runner: ProcessRunner,
    *,
    executable: Path,
    workspace: Workspace,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
    expected_digest: str,
) -> None:
    """Re-verify the control-plane digest before the next `opencode run`.

    Any drift -- including a call failure that prevents recomputing the
    digest at all, e.g. because the coder edited an agent definition that
    happens to live inside the target -- is `PROTOCOL_ERROR` with code
    `opencode.control_plane_drift`, never silently accepted or downgraded
    back to `PreflightError` (ADR-005, System Design SS18.2).
    """

    try:
        config, agents = _fetch_raw_control_plane(
            process_runner,
            executable,
            workspace=workspace,
            utility_timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
        )
    except PreflightError as error:
        raise ProtocolError(
            "opencode.control_plane_drift",
            "The OpenCode control plane could not be reconfirmed before the next run.",
            causes=(error,),
        ) from None

    digest = compute_control_plane_digest(config=config, agents=agents)
    if digest != expected_digest:
        raise ProtocolError(
            "opencode.control_plane_drift",
            "The OpenCode control-plane digest changed since the initial preflight.",
        )


def verify_agent_identity(
    role: AgentRole, export: dict[str, object], *, digest: str
) -> AgentIdentityEvidence:
    """Verify every top-level assistant message's `info.agent` against `role`.

    The export must have a `messages` array; among entries whose
    `info.role` is `"assistant"`, every one's `info.agent` must name the
    same agent, and at least one such message must exist -- several
    assistant messages from the same agent (a normal multi-turn exchange)
    are fine, but disagreement is not. `task` is already denied at
    preflight (M07-02), so a genuine subagent message should never appear;
    if it -- or a schema surprise, a missing agent field, or a silent
    fallback to a different agent -- ever does, this is `ProtocolError`,
    never a fallback or best-effort inference (ADR-005, System Design
    SS10.5).
    """

    messages = export.get("messages")
    if not isinstance(messages, list):
        raise ProtocolError(
            "opencode.export_invalid_schema",
            "opencode export has no messages array.",
        )

    assistant_agents: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise ProtocolError(
                "opencode.export_invalid_schema",
                "opencode export contains a non-object message.",
            )
        info = message.get("info")
        if not isinstance(info, dict):
            raise ProtocolError(
                "opencode.export_invalid_schema",
                "An opencode export message has no info object.",
            )
        if info.get("role") != "assistant":
            continue
        agent = info.get("agent")
        if not isinstance(agent, str) or not agent:
            raise ProtocolError(
                "opencode.export_agent_missing",
                "An assistant message has no non-empty info.agent.",
            )
        assistant_agents.add(agent)

    if not assistant_agents:
        raise ProtocolError(
            "opencode.export_no_assistant_message",
            "opencode export has no top-level assistant message.",
        )
    if len(assistant_agents) > 1:
        raise ProtocolError(
            "opencode.export_ambiguous_agent",
            "opencode export's assistant messages disagree on info.agent.",
        )

    verified_agent = next(iter(assistant_agents))
    requested_agent = _ROLE_TOKENS[role]
    if verified_agent != requested_agent:
        raise ProtocolError(
            "opencode.export_agent_mismatch",
            f"opencode export shows agent {verified_agent!r}, not the "
            f"requested {requested_agent!r}.",
        )

    return AgentIdentityEvidence(
        requested_agent=requested_agent,
        verified_agent=verified_agent,
        method=_EXPORT_METHOD,
        version=CANDIDATE_OPENCODE_VERSION,
        digest=digest,
    )


def run_export_and_verify_identity(
    process_runner: ProcessRunner,
    *,
    executable: Path,
    workspace: Workspace,
    role: AgentRole,
    session_id: str,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> AgentIdentityEvidence:
    """Run `export <session_id> --sanitize` and verify the effective agent.

    `session_id` is expected to already be the single ID
    `decode_run_transport` extracted from the preceding `opencode run`
    (System Design SS10.5); that step is where "zero or more than one
    session ID" is already resolved to a `ProtocolError`, so this function
    does not re-derive or disambiguate it, only refuses to proceed with an
    empty one. The raw export is parsed in memory, bounded by the same
    utility output limit as every other preflight/utility call, and never
    persisted -- only the digest of what was examined is (System Design
    SS10.5, SS18.3).
    """

    if not session_id:
        raise ProtocolError(
            "opencode.export_missing_session_id",
            "No session ID is available to export.",
        )

    result, sink = _run_utility(
        process_runner,
        executable,
        ("export", session_id, "--sanitize"),
        log_name="export-sanitize.log",
        cwd=workspace.root,
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    _require_process_succeeded(
        result,
        error_cls=ProtocolError,
        code="opencode.export_call_failed",
        message="opencode export --sanitize did not complete successfully.",
    )
    raw_bytes = sink.bytes_for("stdout")
    export_text = _decode_or_raise(
        sink, error_cls=ProtocolError, code="opencode.export_call_failed"
    )
    export = _parse_json_object(
        export_text,
        error_cls=ProtocolError,
        code="opencode.export_invalid_schema",
        what="opencode export",
    )
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return verify_agent_identity(role, export, digest=digest)


__all__ = (
    "CANDIDATE_OPENCODE_VERSION",
    "FORBIDDEN_RUN_FLAGS",
    "MAX_NDJSON_LINES",
    "RUN_OUTPUT_LIMIT_BYTES",
    "AgentIdentityEvidence",
    "ControlPlaneEvidence",
    "TransportResult",
    "build_run_spec",
    "check_debug_agent",
    "check_debug_config",
    "check_no_forbidden_flags",
    "check_run_help_capability",
    "check_version",
    "compute_control_plane_digest",
    "decode_run_output",
    "decode_run_transport",
    "open_run_capture_sink",
    "recheck_control_plane",
    "redact_command_for_display",
    "resolve_executable",
    "run_export_and_verify_identity",
    "run_preflight",
    "verify_agent_identity",
)

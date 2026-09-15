"""Integrity, inventory, and provenance tests for the offline OpenCode
`1.17.18` fixture pack (M07-01).

This milestone builds evidence only: `tests/fixtures/opencode/1.17.18/` and
its `MANIFEST.json`. There is no `opencode_tools.opencode` adapter yet -- the
transport/capability/identity behavior the pack will anchor is implemented
and tested by later M07 work packages. What this module verifies is that the
pack itself is complete, internally consistent, attributable to the exact
candidate version, free of obvious secrets, and loadable deterministically
offline (System Design SS20.4, ADR-005).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

import pytest

from opencode_tools.domain import (
    AgentRole,
    GitSafetyStatus,
    IssueLocator,
    PersistenceStatus,
    ProcessSpec,
    ProviderRetryConfig,
    RepositoryIdentity,
    RunOutcome,
    Workspace,
)
from opencode_tools.errors import PreflightError, ProtocolError
from opencode_tools.opencode import (
    CANDIDATE_OPENCODE_VERSION,
    FORBIDDEN_RUN_FLAGS,
    MAX_NDJSON_LINES,
    RUN_OUTPUT_LIMIT_BYTES,
    AgentIdentityEvidence,
    ControlPlaneEvidence,
    TransportResult,
    build_run_spec,
    check_debug_agent,
    check_debug_config,
    check_no_forbidden_flags,
    check_run_help_capability,
    check_version,
    compute_control_plane_digest,
    decode_run_output,
    decode_run_transport,
    open_run_capture_sink,
    redact_command_for_display,
    resolve_executable,
    verify_agent_identity,
)
from opencode_tools.protocol import parse_agent_response
from opencode_tools.retry import decide_retry

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES_ROOT: Final = REPO_ROOT / "tests" / "fixtures" / "opencode" / "1.17.18"
MANIFEST_PATH: Final = FIXTURES_ROOT / "MANIFEST.json"
COMPATIBILITY_DOC_PATH: Final = REPO_ROOT / "docs" / "compatibility.md"

OPENCODE_VERSION: Final = "1.17.18"

_VALID_KINDS: Final = frozenset({"positive", "negative"})
_VALID_ROLES: Final = frozenset({"architect", "coder", "reviewer"})

# One category per version-sensitive behavior area System Design SS20.4 and
# issue #21 (M07-01) require offline evidence for.
_REQUIRED_CATEGORIES: Final = frozenset(
    {
        "run-transcript",
        "run-process-exit",
        "provider-trusted",
        "provider-lookalike",
        "transport-malformed",
        "debug-config",
        "debug-agent",
        "export-identity",
    }
)

# Fixtures whose entire point is to be malformed at the line/byte level; every
# other `.ndjson` fixture must be well-formed line-delimited JSON so a typo in
# a "clean" fixture is caught here rather than silently breaking a later
# milestone's parser tests.
_DELIBERATELY_INVALID_JSON_LINES: Final = frozenset(
    {"malformed/invalid-json-line.ndjson"}
)

_SECRET_PATTERNS: Final = tuple(
    re.compile(pattern)
    for pattern in (
        r"AKIA[0-9A-Z]{16}",
        r"gh[pousr]_[A-Za-z0-9]{36,}",
        r"sk-[A-Za-z0-9]{20,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
)


def _load_manifest() -> dict[str, object]:
    with MANIFEST_PATH.open("rb") as manifest_file:
        return cast(dict[str, object], json.load(manifest_file))


def _require_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _require_str(value: object) -> str:
    assert isinstance(value, str)
    return value


def _fixture_entries(manifest: dict[str, object]) -> tuple[dict[str, object], ...]:
    entries = manifest["fixtures"]
    assert isinstance(entries, list)
    return tuple(_require_dict(entry) for entry in cast(list[object], entries))


def _entry_path(entry: dict[str, object]) -> str:
    path = _require_str(entry["path"])
    assert path
    return path


def _all_fixture_files() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in FIXTURES_ROOT.rglob("*")
            if path.is_file() and path.name != "MANIFEST.json"
        )
    )


# --- version and status attribution -----------------------------------------


def test_manifest_declares_the_exact_candidate_version() -> None:
    manifest = _load_manifest()
    assert manifest["opencode_version"] == OPENCODE_VERSION
    assert manifest["compatibility_status"] == "candidate"


def test_manifest_records_verifiable_pack_wide_provenance() -> None:
    manifest = _load_manifest()
    provenance = _require_dict(manifest["provenance"])

    method = _require_str(provenance["method"])
    assert method.strip()

    references = provenance["references"]
    assert isinstance(references, list) and references
    for reference in cast(list[object], references):
        assert isinstance(reference, str) and reference.strip()


# --- inventory ---------------------------------------------------------------


def test_every_fixture_file_on_disk_is_listed_in_the_manifest_exactly_once() -> None:
    manifest = _load_manifest()
    manifest_paths = [_entry_path(entry) for entry in _fixture_entries(manifest)]
    assert len(manifest_paths) == len(set(manifest_paths))

    disk_paths = {str(path.relative_to(FIXTURES_ROOT)) for path in _all_fixture_files()}
    assert set(manifest_paths) == disk_paths


def test_manifest_covers_every_required_fixture_category() -> None:
    manifest = _load_manifest()
    categories = {
        _require_str(entry["category"]) for entry in _fixture_entries(manifest)
    }
    missing = _REQUIRED_CATEGORIES - categories
    assert not missing, (
        f"fixture pack is missing required categories: {sorted(missing)}"
    )


def test_every_fixture_entry_has_verifiable_attribution() -> None:
    manifest = _load_manifest()
    for entry in _fixture_entries(manifest):
        assert entry["kind"] in _VALID_KINDS
        assert entry["role"] is None or entry["role"] in _VALID_ROLES

        category = _require_str(entry["category"])
        assert category.strip()

        description = _require_str(entry["description"])
        assert description.strip()

        sha256 = _require_str(entry["sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", sha256)


def test_pack_contains_both_positive_and_negative_trust_boundary_cases() -> None:
    manifest = _load_manifest()
    kinds = {_require_str(entry["kind"]) for entry in _fixture_entries(manifest)}
    assert kinds == _VALID_KINDS

    provider_categories = {
        _require_str(entry["category"])
        for entry in _fixture_entries(manifest)
        if _require_str(entry["category"]).startswith("provider-")
    }
    assert provider_categories == {"provider-trusted", "provider-lookalike"}


# --- integrity -----------------------------------------------------------------


def test_every_fixture_digest_matches_its_manifest_entry() -> None:
    manifest = _load_manifest()
    for entry in _fixture_entries(manifest):
        path = FIXTURES_ROOT / _entry_path(entry)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == entry["sha256"], (
            f"{entry['path']} content does not match its recorded sha256; "
            "fixtures are immutable evidence and must not drift silently"
        )


def test_json_fixtures_under_debug_and_export_are_well_formed() -> None:
    for directory in ("debug", "export"):
        for path in sorted((FIXTURES_ROOT / directory).glob("*.json")):
            json.loads(path.read_text(encoding="utf-8"))


def test_ndjson_fixtures_are_line_delimited_json_unless_deliberately_malformed() -> (
    None
):
    for directory in ("run", "provider"):
        for path in sorted((FIXTURES_ROOT / directory).glob("*.ndjson")):
            for line in path.read_text(encoding="utf-8").splitlines():
                json.loads(line)

    for path in sorted((FIXTURES_ROOT / "malformed").glob("*.ndjson")):
        relative = str(path.relative_to(FIXTURES_ROOT))
        lines = path.read_text(encoding="utf-8").splitlines()
        if relative in _DELIBERATELY_INVALID_JSON_LINES:
            with pytest.raises(json.JSONDecodeError):
                for line in lines:
                    json.loads(line)
        else:
            for line in lines:
                json.loads(line)


def test_non_utf8_fixture_is_actually_invalid_utf8() -> None:
    path = FIXTURES_ROOT / "malformed" / "non-utf8-bytes.bin"
    with pytest.raises(UnicodeDecodeError):
        path.read_bytes().decode("utf-8")


def test_agent_fallback_warning_fixture_mixes_a_non_json_line_into_the_stream() -> None:
    path = FIXTURES_ROOT / "malformed" / "agent-fallback-warning.stdout.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2

    non_json_line_count = 0
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            non_json_line_count += 1
    assert non_json_line_count >= 1


# --- offline determinism -------------------------------------------------------


def test_fixture_pack_loads_deterministically_offline() -> None:
    first = {
        str(path.relative_to(FIXTURES_ROOT)): path.read_bytes()
        for path in _all_fixture_files()
    }
    second = {
        str(path.relative_to(FIXTURES_ROOT)): path.read_bytes()
        for path in _all_fixture_files()
    }
    assert first == second
    assert _load_manifest() == _load_manifest()


# --- no secrets ------------------------------------------------------------------


def test_no_fixture_contains_an_obvious_secret_pattern() -> None:
    for path in (*_all_fixture_files(), MANIFEST_PATH):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # the deliberately invalid-UTF-8 fixture has no text to scan
        for pattern in _SECRET_PATTERNS:
            assert not pattern.search(text), (
                f"{path} matches secret pattern {pattern.pattern}"
            )


# --- compatibility documentation ------------------------------------------------


def test_compatibility_doc_declares_the_version_candidate_not_supported() -> None:
    doc = COMPATIBILITY_DOC_PATH.read_text(encoding="utf-8")
    assert OPENCODE_VERSION in doc
    assert "candidate" in doc.lower()
    assert "not yet supported" in doc.lower() or "not supported" in doc.lower()


# =============================================================================
# M07-02: exact-version and capability/control-plane preflight
# =============================================================================


def _fixture_json(relative_path: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((FIXTURES_ROOT / relative_path).read_text(encoding="utf-8")),
    )


# --- resolve_executable -----------------------------------------------------


def test_resolve_executable_returns_the_resolved_which_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(
        shutil, "which", lambda name: str(fake) if name == "opencode" else None
    )
    assert resolve_executable() == fake.resolve()


def test_resolve_executable_fails_closed_when_not_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(PreflightError) as exc_info:
        resolve_executable()
    assert exc_info.value.code == "opencode.executable_not_found"


# --- check_version -----------------------------------------------------------


def test_check_version_accepts_the_exact_candidate() -> None:
    assert (
        check_version(f"{CANDIDATE_OPENCODE_VERSION}\n") == CANDIDATE_OPENCODE_VERSION
    )


@pytest.mark.parametrize(
    "raw_output", ["1.17.19", "1.17.1", "opencode 1.17.18", "v1.17.18", ""]
)
def test_check_version_rejects_anything_but_an_exact_match(raw_output: str) -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_version(raw_output)
    assert exc_info.value.code == "opencode.version_mismatch"


# --- check_run_help_capability -----------------------------------------------


def test_check_run_help_capability_accepts_all_required_tokens() -> None:
    check_run_help_capability(
        "Usage: opencode run [--agent <name>] [--format json] [--dir <path>]"
    )


@pytest.mark.parametrize(
    "raw_output",
    [
        "Usage: opencode run [--format json] [--dir <path>]",
        "Usage: opencode run [--agent <name>] [--dir <path>]",
        "Usage: opencode run [--agent <name>] [--format json]",
        "",
    ],
)
def test_check_run_help_capability_rejects_missing_tokens(raw_output: str) -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_run_help_capability(raw_output)
    assert exc_info.value.code == "opencode.capability_missing"


# --- check_debug_config -------------------------------------------------------


def test_check_debug_config_accepts_the_baseline_fixture() -> None:
    check_debug_config(_fixture_json("debug/config-baseline.json"))


def test_check_debug_config_rejects_the_auto_share_fixture() -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_debug_config(_fixture_json("debug/config-auto-share-enabled.json"))
    assert exc_info.value.code == "opencode.debug_config_rejected"


# --- check_debug_agent ---------------------------------------------------------


@pytest.mark.parametrize(
    "role,fixture_name",
    [
        (AgentRole.ARCHITECT, "debug/agent-architect-baseline.json"),
        (AgentRole.CODER, "debug/agent-coder-baseline.json"),
        (AgentRole.REVIEWER, "debug/agent-reviewer-baseline.json"),
    ],
)
def test_check_debug_agent_accepts_each_role_baseline(
    role: AgentRole, fixture_name: str
) -> None:
    check_debug_agent(role, _fixture_json(fixture_name))


@pytest.mark.parametrize(
    "role,fixture_name,expected_code",
    [
        (
            AgentRole.ARCHITECT,
            "debug/agent-architect-ask-enabled.json",
            "opencode.debug_agent_rejected",
        ),
        (
            AgentRole.CODER,
            "debug/agent-coder-task-enabled.json",
            "opencode.debug_agent_rejected",
        ),
        (
            AgentRole.REVIEWER,
            "debug/agent-reviewer-subagent-mode.json",
            "opencode.debug_agent_rejected",
        ),
        (
            AgentRole.ARCHITECT,
            "debug/agent-architect-permissive-edit.json",
            "opencode.debug_agent_rejected",
        ),
        (
            AgentRole.ARCHITECT,
            "debug/agent-missing.json",
            "opencode.debug_agent_identity_mismatch",
        ),
    ],
)
def test_check_debug_agent_rejects_each_negative_fixture(
    role: AgentRole, fixture_name: str, expected_code: str
) -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(role, _fixture_json(fixture_name))
    assert exc_info.value.code == expected_code


def test_check_debug_agent_rejects_a_fallback_to_a_different_named_agent() -> None:
    fallback_agent: dict[str, object] = {
        "name": "general",
        "mode": "primary",
        "tools": {"ask": False, "task": False},
        "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
    }
    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, fallback_agent)
    assert exc_info.value.code == "opencode.debug_agent_identity_mismatch"


# --- compute_control_plane_digest ---------------------------------------------


def _baseline_agents() -> dict[AgentRole, dict[str, object]]:
    return {
        AgentRole.ARCHITECT: _fixture_json("debug/agent-architect-baseline.json"),
        AgentRole.CODER: _fixture_json("debug/agent-coder-baseline.json"),
        AgentRole.REVIEWER: _fixture_json("debug/agent-reviewer-baseline.json"),
    }


def test_compute_control_plane_digest_is_stable_regardless_of_key_order() -> None:
    agents = _baseline_agents()
    config_a = {"share": "manual", "autoshare": False}
    config_b = {"autoshare": False, "share": "manual"}

    assert compute_control_plane_digest(
        config=config_a, agents=agents
    ) == compute_control_plane_digest(config=config_b, agents=agents)


def test_compute_control_plane_digest_changes_when_content_changes() -> None:
    agents = _baseline_agents()
    digest_before = compute_control_plane_digest(
        config={"share": "manual"}, agents=agents
    )
    digest_after = compute_control_plane_digest(config={"share": "auto"}, agents=agents)
    assert digest_before != digest_after


# --- ControlPlaneEvidence ------------------------------------------------------


def test_control_plane_evidence_rejects_a_non_candidate_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="candidate version"):
        ControlPlaneEvidence(
            version="1.17.19",
            executable=tmp_path / "opencode",
            control_plane_digest="digest",
        )


def test_control_plane_evidence_rejects_a_relative_executable() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ControlPlaneEvidence(
            version=CANDIDATE_OPENCODE_VERSION,
            executable=Path("opencode"),
            control_plane_digest="digest",
        )


def test_control_plane_evidence_rejects_an_empty_digest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="digest"):
        ControlPlaneEvidence(
            version=CANDIDATE_OPENCODE_VERSION,
            executable=tmp_path / "opencode",
            control_plane_digest="",
        )


# =============================================================================
# M07-03: command builder and flag deny-list
# =============================================================================


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(root=tmp_path)


# --- build_run_spec ------------------------------------------------------------


@pytest.mark.parametrize(
    "role,token",
    [
        (AgentRole.ARCHITECT, "architect"),
        (AgentRole.CODER, "coder"),
        (AgentRole.REVIEWER, "reviewer"),
    ],
)
def test_build_run_spec_produces_the_exact_argv_for_each_role(
    role: AgentRole, token: str, tmp_path: Path
) -> None:
    executable = tmp_path / "opencode"
    workspace = _workspace(tmp_path / "workspace")

    spec = build_run_spec(
        executable,
        role,
        "the prompt",
        workspace,
        timeout_seconds=30,
        termination_grace_seconds=5,
    )

    assert spec.argv == (
        str(executable),
        "run",
        "--agent",
        token,
        "--format",
        "json",
        "--dir",
        str(workspace.root),
    )
    assert spec.cwd == workspace.root
    assert spec.stdin == "the prompt"


def test_build_run_spec_never_includes_a_forbidden_flag(tmp_path: Path) -> None:
    executable = tmp_path / "opencode"
    workspace = _workspace(tmp_path / "workspace")

    spec = build_run_spec(
        executable,
        AgentRole.CODER,
        "the prompt",
        workspace,
        timeout_seconds=30,
        termination_grace_seconds=5,
    )

    assert not any(flag in spec.argv for flag in FORBIDDEN_RUN_FLAGS)
    assert not any("--model" in token for token in spec.argv)


def test_build_run_spec_rejects_a_relative_executable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    with pytest.raises(ValueError, match="absolute"):
        build_run_spec(
            Path("opencode"),
            AgentRole.ARCHITECT,
            "the prompt",
            workspace,
            timeout_seconds=30,
            termination_grace_seconds=5,
        )


# --- check_no_forbidden_flags ---------------------------------------------------


def test_check_no_forbidden_flags_accepts_a_clean_argv() -> None:
    check_no_forbidden_flags(("/usr/bin/opencode", "run", "--agent", "coder"))


@pytest.mark.parametrize("flag", FORBIDDEN_RUN_FLAGS)
def test_check_no_forbidden_flags_rejects_each_forbidden_flag(flag: str) -> None:
    with pytest.raises(AssertionError):
        check_no_forbidden_flags(("/usr/bin/opencode", "run", flag))


# --- redact_command_for_display -------------------------------------------------


def test_redact_command_for_display_appends_marker_when_stdin_present(
    tmp_path: Path,
) -> None:
    spec = ProcessSpec(
        argv=("/usr/bin/opencode", "run", "--agent", "coder"),
        cwd=tmp_path,
        stdin="the prompt",
        timeout_seconds=30,
        termination_grace_seconds=5,
    )
    assert redact_command_for_display(spec) == (
        "/usr/bin/opencode",
        "run",
        "--agent",
        "coder",
        "<PROMPT_REDACTED>",
    )
    assert "the prompt" not in redact_command_for_display(spec)


def test_redact_command_for_display_omits_marker_when_stdin_absent(
    tmp_path: Path,
) -> None:
    spec = ProcessSpec(
        argv=("/usr/bin/opencode", "--version"),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=30,
        termination_grace_seconds=5,
    )
    assert redact_command_for_display(spec) == ("/usr/bin/opencode", "--version")


def test_redact_command_for_display_redacts_credentials_in_argv(
    tmp_path: Path,
) -> None:
    spec = ProcessSpec(
        argv=("/usr/bin/git", "fetch", "https://user:secret@example.com/repo.git"),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=30,
        termination_grace_seconds=5,
    )
    displayed = redact_command_for_display(spec)
    assert "secret" not in " ".join(displayed)


# =============================================================================
# M07-04: NDJSON transport decoder
# =============================================================================

RUN_FIXTURES = FIXTURES_ROOT / "run"
PROVIDER_FIXTURES = FIXTURES_ROOT / "provider"
MALFORMED_FIXTURES = FIXTURES_ROOT / "malformed"
NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- decode_run_transport: one clean terminal text per success fixture ------


@pytest.mark.parametrize(
    "fixture_name,expected_session_id,expected_terminal_suffix",
    [
        (
            "architect-ready-success.ndjson",
            "ses_architect_ready",
            "AGENT_STATUS: READY",
        ),
        ("architect-failed.ndjson", "ses_architect_failed", "AGENT_STATUS: FAILED"),
        (
            "coder-completed-success.ndjson",
            "ses_coder_completed",
            "AGENT_STATUS: COMPLETED",
        ),
        ("coder-failed.ndjson", "ses_coder_failed", "AGENT_STATUS: FAILED"),
        (
            "reviewer-approved-success.ndjson",
            "ses_reviewer_approved",
            "REVIEW_STATUS: APPROVED",
        ),
        (
            "reviewer-changes-required.ndjson",
            "ses_reviewer_changes_required",
            "REVIEW_STATUS: CHANGES_REQUIRED",
        ),
        ("reviewer-failed.ndjson", "ses_reviewer_failed", "AGENT_STATUS: FAILED"),
    ],
)
def test_decode_run_transport_produces_one_terminal_text_per_clean_transcript(
    fixture_name: str, expected_session_id: str, expected_terminal_suffix: str
) -> None:
    result = decode_run_transport(_text(RUN_FIXTURES / fixture_name))
    assert isinstance(result, TransportResult)
    assert result.session_id == expected_session_id
    assert result.terminal_text.endswith(expected_terminal_suffix)


# --- transport, session, and terminal-candidate violations -------------------


@pytest.mark.parametrize(
    "fixture_name,expected_code",
    [
        ("invalid-json-line.ndjson", "opencode.transport_invalid_json"),
        ("unknown-event-type.ndjson", "opencode.transport_unknown_event_type"),
        ("session-id-mismatch.ndjson", "opencode.transport_session_id_mismatch"),
        ("truncated-no-terminal-text.ndjson", "opencode.transport_no_terminal_text"),
        (
            "multi-terminal-candidates.ndjson",
            "opencode.transport_multiple_terminal_candidates",
        ),
    ],
)
def test_decode_run_transport_fails_closed_on_each_malformed_fixture(
    fixture_name: str, expected_code: str
) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport(_text(MALFORMED_FIXTURES / fixture_name))
    assert exc_info.value.code == expected_code


def test_decode_run_transport_fails_closed_on_a_non_json_warning_line() -> None:
    text = _text(MALFORMED_FIXTURES / "agent-fallback-warning.stdout.txt")
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport(text)
    assert exc_info.value.code == "opencode.transport_invalid_json"


def test_decode_run_transport_fails_closed_when_no_events_are_present() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("")
    assert exc_info.value.code == "opencode.transport_no_terminal_text"


# --- AC-035: transport and status spoofing fail closed -----------------------


def test_ac_035_transport_and_status_spoofing_fail_closed() -> None:
    # A malformed (not line-delimited JSON) stream fails closed at decode.
    with pytest.raises(ProtocolError) as invalid_json_error:
        decode_run_transport(_text(MALFORMED_FIXTURES / "invalid-json-line.ndjson"))
    assert invalid_json_error.value.code == "opencode.transport_invalid_json"

    # A truncated stream with no terminal text fails closed, not silently.
    with pytest.raises(ProtocolError) as truncated_error:
        decode_run_transport(
            _text(MALFORMED_FIXTURES / "truncated-no-terminal-text.ndjson")
        )
    assert truncated_error.value.code == "opencode.transport_no_terminal_text"

    # More than one candidate terminal message is rejected, never guessed.
    with pytest.raises(ProtocolError) as multi_terminal_error:
        decode_run_transport(
            _text(MALFORMED_FIXTURES / "multi-terminal-candidates.ndjson")
        )
    assert (
        multi_terminal_error.value.code
        == "opencode.transport_multiple_terminal_candidates"
    )

    # An agent that emits FINAL_STATUS is rejected too -- decode_run_transport
    # has no marker semantics, so this spoofing check lives one boundary
    # further in, at parse_agent_response, which this file also owns.
    locator = IssueLocator(
        RepositoryIdentity(
            host="github.com",
            owner="octocat",
            repository="hello-world",
            source="test",
        ),
        number=1,
    )
    with pytest.raises(ProtocolError) as final_status_error:
        parse_agent_response(
            AgentRole.CODER,
            "FINAL_STATUS: APPROVED\nAGENT_STATUS: COMPLETED",
            issue_locator=locator,
        )
    assert final_status_error.value.code == "protocol.final_status_reserved"

    # None of these PROTOCOL_ERROR-classified failures authorize a provider
    # retry. Every other guard is held favorable so the outcome-type guard
    # is unambiguously what denies it, not some other guard failing too.
    retry_config = ProviderRetryConfig(
        max_attempts=3,
        initial_delay_seconds=1.0,
        multiplier=2.0,
        max_delay_seconds=60.0,
    )
    decision = decide_retry(
        outcome=RunOutcome.PROTOCOL_ERROR,
        provider_diagnostic=None,
        provider_attempt=1,
        role=AgentRole.CODER,
        target_changed=False,
        termination_confirmed=True,
        git_safety_status=GitSafetyStatus.SAFE,
        persistence_status=PersistenceStatus.OK,
        cancellation_requested=False,
        config=retry_config,
    )
    assert decision.should_retry is False


# --- tool/reasoning exclusion and marker-spoofing resistance -----------------


def test_decode_run_transport_excludes_tool_output_from_the_result() -> None:
    result = decode_run_transport(
        _text(MALFORMED_FIXTURES / "tool-output-marker-spoofing.ndjson")
    )
    assert "AGENT_STATUS: READY" not in result.terminal_text
    assert result.terminal_text.endswith("AGENT_STATUS: FAILED")


def test_decode_run_transport_excludes_reasoning_from_the_result() -> None:
    result = decode_run_transport(
        _text(MALFORMED_FIXTURES / "reasoning-marker-spoofing.ndjson")
    )
    assert "AGENT_STATUS: COMPLETED" not in result.terminal_text
    assert result.terminal_text.endswith("AGENT_STATUS: FAILED")


def test_decode_run_transport_excludes_a_provider_error_event_from_the_result() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport(_text(PROVIDER_FIXTURES / "http-429-rate-limit.ndjson"))
    assert exc_info.value.code == "opencode.transport_no_terminal_text"


# --- CRLF/CR normalization ----------------------------------------------------


def test_decode_run_transport_normalizes_crlf_to_lf() -> None:
    crlf_text = _text(RUN_FIXTURES / "architect-ready-success.ndjson").replace(
        "\n", "\r\n"
    )
    result = decode_run_transport(crlf_text)
    assert result.terminal_text.endswith("AGENT_STATUS: READY")


def test_decode_run_transport_normalizes_bare_cr_to_lf() -> None:
    cr_text = _text(RUN_FIXTURES / "architect-ready-success.ndjson").replace("\n", "\r")
    result = decode_run_transport(cr_text)
    assert result.terminal_text.endswith("AGENT_STATUS: READY")


# --- dimension limits ----------------------------------------------------------


def test_decode_run_transport_rejects_more_than_the_line_limit() -> None:
    filler_line = json.dumps(
        {
            "type": "message.part.updated",
            "sessionID": "ses_limit",
            "part": {"id": "prt", "messageID": "msg", "type": "reasoning", "text": "x"},
        }
    )
    text = "\n".join([filler_line] * (MAX_NDJSON_LINES + 1))
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport(text)
    assert exc_info.value.code == "opencode.transport_output_too_large"


# --- decode_run_output: byte-level overflow and UTF-8 wrapper ----------------


def test_decode_run_output_rejects_overflowed_stdout() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_output(b"irrelevant", overflowed=True)
    assert exc_info.value.code == "opencode.transport_output_too_large"


def test_decode_run_output_rejects_invalid_utf8() -> None:
    invalid_bytes = (MALFORMED_FIXTURES / "non-utf8-bytes.bin").read_bytes()
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_output(invalid_bytes, overflowed=False)
    assert exc_info.value.code == "opencode.transport_invalid_utf8"


def test_decode_run_output_decodes_valid_bytes() -> None:
    raw = (RUN_FIXTURES / "coder-completed-success.ndjson").read_bytes()
    result = decode_run_output(raw, overflowed=False)
    assert result.terminal_text.endswith("AGENT_STATUS: COMPLETED")


# --- open_run_capture_sink -----------------------------------------------------


def test_open_run_capture_sink_uses_the_run_size_limit() -> None:
    sink = open_run_capture_sink("run.log")
    assert sink.path == Path("run.log")

    sink.write("stdout", b"x" * RUN_OUTPUT_LIMIT_BYTES, NOW)
    assert not sink.overflowed("stdout")

    sink.write("stdout", b"x", NOW)
    assert sink.overflowed("stdout")


# --- TransportResult -----------------------------------------------------------


def test_transport_result_rejects_an_empty_session_id() -> None:
    with pytest.raises(ValueError, match="session_id"):
        TransportResult(session_id="", terminal_text="AGENT_STATUS: COMPLETED")


# =============================================================================
# M07-05: effective agent identity via sanitized export
# =============================================================================

EXPORT_FIXTURES = FIXTURES_ROOT / "export"


def _export_fixture(name: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((EXPORT_FIXTURES / name).read_text(encoding="utf-8")),
    )


# --- verify_agent_identity: correct agent -------------------------------------


@pytest.mark.parametrize(
    "role,fixture_name",
    [
        (AgentRole.ARCHITECT, "architect-correct-agent.json"),
        (AgentRole.CODER, "coder-correct-agent.json"),
        (AgentRole.REVIEWER, "reviewer-correct-agent.json"),
    ],
)
def test_verify_agent_identity_accepts_the_correct_agent_fixture(
    role: AgentRole, fixture_name: str
) -> None:
    evidence = verify_agent_identity(
        role, _export_fixture(fixture_name), digest="abc123"
    )
    assert isinstance(evidence, AgentIdentityEvidence)
    assert evidence.requested_agent == evidence.verified_agent == _role_token(role)
    assert evidence.method == "sanitized_session_export"
    assert evidence.version == CANDIDATE_OPENCODE_VERSION
    assert evidence.digest == "abc123"


def _role_token(role: AgentRole) -> str:
    return {
        AgentRole.ARCHITECT: "architect",
        AgentRole.CODER: "coder",
        AgentRole.REVIEWER: "reviewer",
    }[role]


def test_verify_agent_identity_accepts_multiple_messages_from_the_same_agent() -> None:
    export: dict[str, object] = {
        "sessionID": "ses_multi_message",
        "messages": [
            {
                "info": {"id": "msg_1", "role": "assistant", "agent": "coder"},
                "parts": [{"type": "tool", "tool": "bash"}],
            },
            {
                "info": {"id": "msg_2", "role": "assistant", "agent": "coder"},
                "parts": [{"type": "text", "text": "AGENT_STATUS: COMPLETED"}],
            },
        ],
    }
    evidence = verify_agent_identity(AgentRole.CODER, export, digest="d")
    assert evidence.verified_agent == "coder"


# --- verify_agent_identity: negative and trust-boundary fixtures -------------


def test_verify_agent_identity_rejects_a_mismatched_agent() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        verify_agent_identity(
            AgentRole.ARCHITECT, _export_fixture("agent-mismatch.json"), digest="d"
        )
    assert exc_info.value.code == "opencode.export_agent_mismatch"


def test_verify_agent_identity_rejects_a_missing_agent_field() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        verify_agent_identity(
            AgentRole.CODER, _export_fixture("agent-field-missing.json"), digest="d"
        )
    assert exc_info.value.code == "opencode.export_agent_missing"


def test_verify_agent_identity_rejects_zero_assistant_messages() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        verify_agent_identity(
            AgentRole.CODER, _export_fixture("zero-assistant-messages.json"), digest="d"
        )
    assert exc_info.value.code == "opencode.export_no_assistant_message"


def test_verify_agent_identity_rejects_an_ambiguous_fallback_export() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        verify_agent_identity(
            AgentRole.CODER,
            _export_fixture("ambiguous-multiple-assistant-agents.json"),
            digest="d",
        )
    assert exc_info.value.code == "opencode.export_ambiguous_agent"


@pytest.mark.parametrize(
    "export",
    [
        {"sessionID": "s"},
        {"sessionID": "s", "messages": "not-a-list"},
        {"sessionID": "s", "messages": ["not-an-object"]},
        {"sessionID": "s", "messages": [{"info": "not-an-object"}]},
    ],
)
def test_verify_agent_identity_rejects_an_unexpected_schema(
    export: dict[str, object],
) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        verify_agent_identity(AgentRole.CODER, export, digest="d")
    assert exc_info.value.code == "opencode.export_invalid_schema"


# --- AgentIdentityEvidence -----------------------------------------------------


def test_agent_identity_evidence_rejects_a_mismatched_pair() -> None:
    with pytest.raises(ValueError, match="requested_agent and verified_agent"):
        AgentIdentityEvidence(
            requested_agent="architect",
            verified_agent="coder",
            method="sanitized_session_export",
            version=CANDIDATE_OPENCODE_VERSION,
            digest="d",
        )


def test_agent_identity_evidence_rejects_an_empty_field() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        AgentIdentityEvidence(
            requested_agent="architect",
            verified_agent="architect",
            method="sanitized_session_export",
            version=CANDIDATE_OPENCODE_VERSION,
            digest="",
        )

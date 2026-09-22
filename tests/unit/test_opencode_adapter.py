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
    AgentStatus,
    GitSafetyStatus,
    IssueLocator,
    PersistenceStatus,
    PipelinePhase,
    ProcessSpec,
    ProviderRetryConfig,
    RepositoryIdentity,
    RunOutcome,
    Workspace,
)
from opencode_tools.errors import PreflightError, ProtocolError
from opencode_tools.opencode import (
    _CODER_BASH_PERMISSION_CONFIG,
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
from opencode_tools.state_machine import (
    PipelineAction,
    PipelineState,
    TransitionEvent,
    TransitionEventKind,
    transition,
)

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


def test_manifest_declares_the_exact_supported_version() -> None:
    manifest = _load_manifest()
    assert manifest["opencode_version"] == OPENCODE_VERSION
    assert manifest["compatibility_status"] == "supported"


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


def test_compatibility_doc_declares_the_version_supported() -> None:
    doc = COMPATIBILITY_DOC_PATH.read_text(encoding="utf-8")
    assert OPENCODE_VERSION in doc
    assert "status: supported" in doc.lower()


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
    # Modeled on the real `1.17.18` binary's actual `run --help` shape
    # (confirmed live, M15-03): "--format" and "json" are several words
    # apart, never adjacent as a literal "--format json" substring -- a
    # contrived, adjacent-tokens fixture string would not have caught the
    # real mismatch this test now guards against.
    check_run_help_capability(
        "      --agent        agent to use                            [string]\n"
        "      --format       format: default (formatted) or json (raw JSON events)\n"
        '                     [string] [choices: "default", "json"]\n'
        "      --dir          directory to run in                     [string]\n"
    )


@pytest.mark.parametrize(
    "raw_output",
    [
        "Usage: opencode run [--format json] [--dir <path>]",
        "Usage: opencode run [--agent <name>] [--dir <path>]",
        "Usage: opencode run [--agent <name>] [--format] [--dir <path>]",
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
            "debug/agent-architect-permissive-bash.json",
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
        "tools": {"question": False, "task": False},
        "permission": [
            {"permission": "*", "action": "allow", "pattern": "*"},
            {"permission": "edit", "action": "deny", "pattern": "*"},
            {"permission": "bash", "action": "deny", "pattern": "*"},
            {"permission": "webfetch", "action": "deny", "pattern": "*"},
        ],
    }
    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, fallback_agent)
    assert exc_info.value.code == "opencode.debug_agent_identity_mismatch"


def test_check_debug_agent_last_matching_rule_wins_over_an_earlier_broad_one() -> None:
    # A real 1.17.18 response's permission list is ordered and resolved
    # last-match-wins (https://opencode.ai/docs/permissions/), not a flat
    # dict: an earlier, broader "allow" for edit must lose to a later,
    # more specific "deny" -- the exact shape already exercised implicitly
    # by debug/agent-architect-baseline.json, asserted explicitly here.
    agent: dict[str, object] = {
        "name": "architect",
        "mode": "primary",
        "tools": {"question": False, "task": False},
        "permission": [
            {"permission": "*", "action": "allow", "pattern": "*"},
            {"permission": "edit", "action": "allow", "pattern": "*.md"},
            {"permission": "edit", "action": "deny", "pattern": "*"},
            {"permission": "bash", "action": "deny", "pattern": "*"},
            {"permission": "bash", "action": "allow", "pattern": "gh issue view *"},
            {"permission": "webfetch", "action": "deny", "pattern": "*"},
        ],
    }
    check_debug_agent(AgentRole.ARCHITECT, agent)


def test_check_debug_agent_ignores_unrelated_and_machine_specific_rules() -> None:
    # Rules for permission kinds outside the reviewed baseline (read,
    # doom_loop, plan_enter/exit, and external_directory entries a user's
    # own global OpenCode config can add, tied to paths that exist only on
    # that machine) must never affect whether edit/bash/webfetch match the
    # baseline, in any position in the list.
    agent: dict[str, object] = {
        "name": "coder",
        "mode": "primary",
        "tools": {"question": False, "task": False},
        "permission": [
            {"permission": "*", "action": "allow", "pattern": "*"},
            {"permission": "doom_loop", "action": "ask", "pattern": "*"},
            {
                "permission": "external_directory",
                "action": "allow",
                "pattern": "/Users/someone/.local/share/opencode/tool-output/*",
            },
            {"permission": "plan_enter", "action": "deny", "pattern": "*"},
            {"permission": "read", "action": "allow", "pattern": "*"},
            {"permission": "edit", "action": "allow", "pattern": "*"},
            *[
                {"permission": "bash", "action": action, "pattern": pattern}
                for pattern, action in _CODER_BASH_PERMISSION_CONFIG.items()
            ],
            {"permission": "plan_exit", "action": "deny", "pattern": "*"},
            {"permission": "webfetch", "action": "deny", "pattern": "*"},
            {
                "permission": "external_directory",
                "action": "allow",
                "pattern": "/Users/someone/.claude/skills/some-skill/*",
            },
        ],
    }
    check_debug_agent(AgentRole.CODER, agent)


def test_check_debug_agent_rejects_when_a_baseline_permission_has_no_rule_at_all() -> (
    None
):
    # No rule anywhere names "webfetch" or the wildcard "*" -- the
    # effective action cannot be determined, so it must fail closed
    # (None can never equal a real baseline action) rather than being
    # treated as an implicit allow or skipped.
    agent: dict[str, object] = {
        "name": "architect",
        "mode": "primary",
        "tools": {"question": False, "task": False},
        "permission": [
            {"permission": "edit", "action": "deny", "pattern": "*"},
            {"permission": "bash", "action": "deny", "pattern": "*"},
        ],
    }
    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, agent)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


def test_check_debug_agent_rejects_a_malformed_permission_rule_entry() -> None:
    agent: dict[str, object] = {
        "name": "architect",
        "mode": "primary",
        "tools": {"question": False, "task": False},
        "permission": [
            {"permission": "*", "action": "allow", "pattern": "*"},
            "not-an-object",
            {"permission": "edit", "action": "deny", "pattern": "*"},
        ],
    }
    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, agent)
    assert exc_info.value.code == "opencode.debug_agent_invalid"


@pytest.mark.parametrize(
    "late_allow_pattern",
    (
        "git restore README.md",
        "git add -A",
        "git cherry-pick deadbeef",
    ),
)
def test_check_debug_agent_rejects_narrow_late_coder_overrides(
    late_allow_pattern: str,
) -> None:
    agent = _fixture_json("debug/agent-coder-baseline.json")
    permission = cast(list[dict[str, object]], agent["permission"])
    permission.append(
        {"permission": "bash", "action": "allow", "pattern": late_allow_pattern}
    )

    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.CODER, agent)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


# --- check_debug_agent: the architect's least-privilege bash policy ----------
#
# Discovered live during the M15-03 re-qualification (2026-09-17):
# `build_architect_prompt` instructs the architect to run `gh issue view`
# itself to discover the issue title/body -- Python never embeds them -- so
# a flat `bash: deny` made the architect structurally unable to ever
# succeed. The fix is least privilege, not a broader flat allow: deny by
# default, with exactly one narrow, reviewed exception.


def _architect_agent_with_bash_rules(
    *extra_bash_rules: dict[str, object],
) -> dict[str, object]:
    return {
        "name": "architect",
        "mode": "primary",
        "tools": {"question": False, "task": False},
        "permission": [
            {"permission": "*", "action": "allow", "pattern": "*"},
            {"permission": "edit", "action": "deny", "pattern": "*"},
            *extra_bash_rules,
            {"permission": "webfetch", "action": "deny", "pattern": "*"},
        ],
    }


def test_check_debug_agent_accepts_the_exact_reviewed_architect_bash_policy() -> None:
    agent = _architect_agent_with_bash_rules(
        {"permission": "bash", "action": "deny", "pattern": "*"},
        {"permission": "bash", "action": "allow", "pattern": "gh issue view *"},
    )
    check_debug_agent(AgentRole.ARCHITECT, agent)  # must not raise


def test_check_debug_agent_rejects_an_architect_missing_the_gh_issue_view_allow() -> (
    None
):
    agent = _architect_agent_with_bash_rules(
        {"permission": "bash", "action": "deny", "pattern": "*"},
    )
    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, agent)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


def test_check_debug_agent_rejects_an_architect_with_a_broader_bash_allow() -> None:
    agent = _architect_agent_with_bash_rules(
        {"permission": "bash", "action": "deny", "pattern": "*"},
        {"permission": "bash", "action": "allow", "pattern": "gh issue view *"},
        {"permission": "bash", "action": "allow", "pattern": "*"},
    )
    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, agent)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


def test_check_debug_agent_rejects_an_architect_with_a_different_allowed_gh_pattern() -> (
    None
):
    agent = _architect_agent_with_bash_rules(
        {"permission": "bash", "action": "deny", "pattern": "*"},
        {"permission": "bash", "action": "allow", "pattern": "gh issue edit *"},
    )
    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, agent)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


def test_check_debug_agent_rejects_an_architect_whose_deny_all_shadows_the_allow() -> (
    None
):
    # Order matters under last-match-wins: a deny-all placed *after* the
    # narrow allow would shadow it for the one command that must succeed.
    agent = _architect_agent_with_bash_rules(
        {"permission": "bash", "action": "allow", "pattern": "gh issue view *"},
        {"permission": "bash", "action": "deny", "pattern": "*"},
    )
    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, agent)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


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


def _architect_agent_with_permission(
    permission: list[object],
) -> dict[AgentRole, dict[str, object]]:
    architect: dict[str, object] = {
        "name": "architect",
        "mode": "primary",
        "tools": {"question": False, "task": False},
        "permission": permission,
    }
    agents = _baseline_agents()
    agents[AgentRole.ARCHITECT] = architect
    return agents


def test_compute_control_plane_digest_is_stable_across_external_directory_reorder() -> (
    None
):
    # Confirmed live during M15-03: successive real `debug agent` calls
    # return the same set of external_directory rules (a user's own
    # global OpenCode config, e.g. installed skill folders) in a
    # different order each time. Two non-overlapping, distinct-prefix
    # patterns reordered must hash identically.
    base: list[object] = [
        {"permission": "*", "action": "allow", "pattern": "*"},
        {"permission": "edit", "action": "deny", "pattern": "*"},
    ]
    order_a: list[object] = [
        *base,
        {"permission": "external_directory", "action": "allow", "pattern": "/a/*"},
        {"permission": "external_directory", "action": "allow", "pattern": "/b/*"},
        {"permission": "external_directory", "action": "allow", "pattern": "/c/*"},
    ]
    order_b: list[object] = [
        *base,
        {"permission": "external_directory", "action": "allow", "pattern": "/c/*"},
        {"permission": "external_directory", "action": "allow", "pattern": "/a/*"},
        {"permission": "external_directory", "action": "allow", "pattern": "/b/*"},
    ]
    digest_a = compute_control_plane_digest(
        config={}, agents=_architect_agent_with_permission(order_a)
    )
    digest_b = compute_control_plane_digest(
        config={}, agents=_architect_agent_with_permission(order_b)
    )
    assert digest_a == digest_b


def test_compute_control_plane_digest_changes_when_a_permission_rule_changes() -> None:
    permission_before: list[object] = [
        {"permission": "*", "action": "allow", "pattern": "*"},
        {"permission": "edit", "action": "deny", "pattern": "*"},
    ]
    permission_after: list[object] = [
        {"permission": "*", "action": "allow", "pattern": "*"},
        {"permission": "edit", "action": "allow", "pattern": "*"},
    ]
    digest_before = compute_control_plane_digest(
        config={}, agents=_architect_agent_with_permission(permission_before)
    )
    digest_after = compute_control_plane_digest(
        config={}, agents=_architect_agent_with_permission(permission_after)
    )
    assert digest_before != digest_after


def test_compute_control_plane_digest_changes_on_overlapping_external_directory_reorder() -> (
    None
):
    # Two external_directory rules for the SAME pattern with different
    # actions are ambiguous to reorder (which one would apply to a real
    # path is exactly a function of their relative order) -- proven
    # overlapping by `_external_directory_patterns_may_overlap`, so this
    # run is never canonicalized and a reordering still registers as
    # drift, per the fail-closed requirement.
    order_a: list[object] = [
        {"permission": "*", "action": "allow", "pattern": "*"},
        {"permission": "external_directory", "action": "ask", "pattern": "/a/*"},
        {"permission": "external_directory", "action": "allow", "pattern": "/a/*"},
    ]
    order_b: list[object] = [
        {"permission": "*", "action": "allow", "pattern": "*"},
        {"permission": "external_directory", "action": "allow", "pattern": "/a/*"},
        {"permission": "external_directory", "action": "ask", "pattern": "/a/*"},
    ]
    digest_a = compute_control_plane_digest(
        config={}, agents=_architect_agent_with_permission(order_a)
    )
    digest_b = compute_control_plane_digest(
        config={}, agents=_architect_agent_with_permission(order_b)
    )
    assert digest_a != digest_b


def test_compute_control_plane_digest_stays_order_sensitive_for_non_external_directory_rules() -> (
    None
):
    # Only external_directory runs are ever canonicalized -- reordering
    # any other permission kind (here, two differently-scoped `read`
    # rules) must still register as drift; their order is meaningfully
    # part of the effective policy and is never touched.
    order_a: list[object] = [
        {"permission": "read", "action": "allow", "pattern": "*"},
        {"permission": "read", "action": "ask", "pattern": "*.env"},
    ]
    order_b: list[object] = [
        {"permission": "read", "action": "ask", "pattern": "*.env"},
        {"permission": "read", "action": "allow", "pattern": "*"},
    ]
    digest_a = compute_control_plane_digest(
        config={}, agents=_architect_agent_with_permission(order_a)
    )
    digest_b = compute_control_plane_digest(
        config={}, agents=_architect_agent_with_permission(order_b)
    )
    assert digest_a != digest_b


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
    "role,token,use_target",
    [
        (AgentRole.ARCHITECT, "architect", False),
        (AgentRole.CODER, "coder", True),
        (AgentRole.REVIEWER, "reviewer", False),
    ],
)
def test_build_run_spec_uses_the_role_specific_context(
    role: AgentRole, token: str, use_target: bool, tmp_path: Path
) -> None:
    executable = tmp_path / "opencode"
    workspace = _workspace(tmp_path / "workspace")
    target_root = workspace.root / "Backend"

    spec = build_run_spec(
        executable,
        role,
        "the prompt",
        workspace,
        target_root=target_root,
        timeout_seconds=30,
        termination_grace_seconds=5,
    )

    expected_directory = target_root if use_target else workspace.root
    assert spec.argv == (
        str(executable),
        "run",
        "--agent",
        token,
        "--format",
        "json",
        "--dir",
        str(expected_directory),
    )
    assert spec.cwd == expected_directory
    assert spec.stdin == "the prompt"
    if use_target:
        assert spec.environment_overrides["OPENCODE_CONFIG_DIR"] == str(
            workspace.root / ".opencode"
        )
    else:
        assert "OPENCODE_CONFIG_DIR" not in spec.environment_overrides


def test_build_run_spec_keeps_single_repo_coder_behavior_unchanged(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "opencode"
    workspace = _workspace(tmp_path / "workspace")

    spec = build_run_spec(
        executable,
        AgentRole.CODER,
        "the prompt",
        workspace,
        target_root=workspace.root,
        timeout_seconds=30,
        termination_grace_seconds=5,
    )

    assert spec.cwd == workspace.root
    assert spec.argv[-1] == str(workspace.root)
    assert "OPENCODE_CONFIG_DIR" not in spec.environment_overrides


def test_build_run_spec_never_includes_a_forbidden_flag(tmp_path: Path) -> None:
    executable = tmp_path / "opencode"
    workspace = _workspace(tmp_path / "workspace")

    spec = build_run_spec(
        executable,
        AgentRole.CODER,
        "the prompt",
        workspace,
        target_root=workspace.root,
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
            target_root=workspace.root,
            timeout_seconds=30,
            termination_grace_seconds=5,
        )


def test_build_run_spec_rejects_a_relative_target(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    with pytest.raises(ValueError, match="target_root must be absolute"):
        build_run_spec(
            tmp_path / "opencode",
            AgentRole.CODER,
            "the prompt",
            workspace,
            target_root=Path("Backend"),
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
        (
            # Genuine NDJSON captured from a real `opencode run --format
            # json` 1.17.18 invocation during M15-03 live qualification --
            # not hand-authored, unlike the other rows in this table.
            "architect-failed.ndjson",
            "ses_f515f27d1ffeeSNk4UDje4MDiK",
            "AGENT_STATUS: FAILED",
        ),
        (
            "coder-completed-success.ndjson",
            "ses_coder_completed",
            "AGENT_STATUS: COMPLETED",
        ),
        (
            # Issue #85: a session that completes more than one message's
            # text; only the last-to-conclude (by step_finish) is terminal.
            "coder-intermediate-then-terminal-text.ndjson",
            "ses_coder_intermediate_then_terminal",
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


# --- M15-03 live-qualification correction: the real 1.17.18 event shape ------
#
# A live `opencode run --format json` capture (M15-03 AC-027 re-qualification,
# 2026-09-17) proved this adapter's original assumption wrong: real NDJSON
# carries the message-lifecycle kind directly as the top-level `type`
# (`step_start`, `text`, `step_finish`, ...), never wrapped in a
# `message.part.updated` envelope. These tests fix that corrected contract so
# it cannot silently regress.


def test_decode_run_transport_rejects_the_old_unproven_message_part_updated_envelope() -> (
    None
):
    text = json.dumps(
        {
            "type": "message.part.updated",
            "sessionID": "ses_old_shape",
            "part": {
                "id": "prt_1",
                "messageID": "msg_1",
                "type": "text",
                "text": "AGENT_STATUS: COMPLETED",
                "time": {"start": 1, "end": 2},
            },
        }
    )
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport(text)
    assert exc_info.value.code == "opencode.transport_unknown_event_type"


@pytest.mark.parametrize("event_type", ["step_start", "tool_use", "error"])
def test_decode_run_transport_recognizes_but_excludes_message_lifecycle_events(
    event_type: str,
) -> None:
    # step_finish is deliberately excluded from this table: unlike these
    # three, its own messageID is semantically load-bearing (issue #85) and
    # is covered by its own stricter tests below instead.
    lines = [
        json.dumps(
            {"type": event_type, "sessionID": "ses_lifecycle", "part": {"id": "p"}}
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": "ses_lifecycle",
                "part": {
                    "id": "prt_2",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
    ]
    result = decode_run_transport("\n".join(lines))
    assert result.session_id == "ses_lifecycle"
    assert result.terminal_text == "AGENT_STATUS: COMPLETED"


def test_decode_run_transport_rejects_a_message_lifecycle_event_with_no_part_object() -> (
    None
):
    text = json.dumps({"type": "step_start", "sessionID": "ses_no_part"})
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport(text)
    assert exc_info.value.code == "opencode.transport_invalid_event"


# --- issue #85: step_finish's part shape is fully validated -----------------


@pytest.mark.parametrize(
    "part",
    [
        pytest.param({"id": "p", "messageID": "msg_1"}, id="missing"),
        pytest.param(
            {"id": "p", "messageID": "msg_1", "type": "step-start"},
            id="wrong_lifecycle_type",
        ),
        pytest.param({"id": "p", "messageID": "msg_1", "type": "text"}, id="text_type"),
        pytest.param(
            {"id": "p", "messageID": "msg_1", "type": 42}, id="wrong_python_type"
        ),
    ],
)
def test_decode_run_transport_rejects_a_step_finish_event_whose_part_type_is_not_step_finish(
    part: dict[str, object],
) -> None:
    lines = [
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": "ses_bad_step_finish_type",
                "part": part,
            }
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": "ses_bad_step_finish_type",
                "part": {
                    "id": "prt_2",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_invalid_event"


def test_decode_run_transport_rejects_a_step_finish_with_wrong_part_type_even_when_the_only_completed_candidate_is_otherwise_clean() -> (
    None
):
    # Mirrors the equivalent messageID test below: a step_finish whose
    # part.type is wrong must never be silently treated as "no lifecycle
    # information present", which would let the stream resolve to a
    # single, unverified candidate exactly the way the pre-#85 adapter did.
    session = "ses_bad_step_finish_type_sole_candidate"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                # part.type is "step-start", not the real "step-finish" --
                # a structurally wrong lifecycle event masquerading as the
                # top-level step_finish event type.
                "part": {"id": "prt_2", "messageID": "msg_1", "type": "step-start"},
            }
        ),
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_invalid_event"


@pytest.mark.parametrize(
    "part",
    [
        pytest.param({"id": "p", "type": "step-finish"}, id="missing"),
        pytest.param(
            {"id": "p", "type": "step-finish", "messageID": ""}, id="empty_string"
        ),
        pytest.param({"id": "p", "type": "step-finish", "messageID": None}, id="null"),
        pytest.param(
            {"id": "p", "type": "step-finish", "messageID": 42}, id="wrong_type"
        ),
    ],
)
def test_decode_run_transport_rejects_a_step_finish_event_with_no_messageID(
    part: dict[str, object],
) -> None:
    lines = [
        json.dumps(
            {"type": "step_finish", "sessionID": "ses_bad_step_finish", "part": part}
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": "ses_bad_step_finish",
                "part": {
                    "id": "prt_2",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_invalid_event"


def test_decode_run_transport_rejects_a_step_finish_with_bad_messageID_even_when_the_only_completed_candidate_is_otherwise_clean() -> (
    None
):
    # A step_finish this malformed must never be silently treated as "no
    # lifecycle information present" -- that would let a stream resolve to
    # a single, unverified candidate exactly the way the pre-#85 adapter
    # did, defeating the whole point of trusting step_finish lifecycle
    # structure at all.
    session = "ses_bad_step_finish_sole_candidate"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                # messageID missing entirely; part.type is otherwise valid
                # so this test isolates messageID validation specifically.
                "part": {"id": "prt_2", "type": "step-finish"},
            }
        ),
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_invalid_event"


def test_decode_run_transport_rejects_a_text_event_whose_part_type_is_not_text() -> (
    None
):
    text = json.dumps(
        {
            "type": "text",
            "sessionID": "ses_wrong_part_type",
            "part": {"id": "prt_1", "messageID": "msg_1", "type": "reasoning"},
        }
    )
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport(text)
    assert exc_info.value.code == "opencode.transport_invalid_event"


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


# --- issue #85: intermediate vs. structurally terminal completed text --------


def test_decode_run_transport_excludes_intermediate_completed_text_from_the_result() -> (
    None
):
    result = decode_run_transport(
        _text(RUN_FIXTURES / "coder-intermediate-then-terminal-text.ndjson")
    )
    assert "Now I have a clear picture" not in result.terminal_text
    assert result.terminal_text == (
        "Implemented the requested module and added regression coverage.\n"
        "AGENT_STATUS: COMPLETED"
    )


def test_decode_run_transport_resolves_multiple_completed_groups_via_last_step_finish() -> (
    None
):
    session = "ses_lifecycle_resolves"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_a",
                    "type": "text",
                    "text": "Intermediate summary, not the terminal response.",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {"id": "prt_2", "messageID": "msg_a", "type": "step-finish"},
            }
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_3",
                    "messageID": "msg_b",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 3, "end": 4},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {"id": "prt_4", "messageID": "msg_b", "type": "step-finish"},
            }
        ),
    ]
    result = decode_run_transport("\n".join(lines))
    assert result.terminal_text == "AGENT_STATUS: COMPLETED"


def test_decode_run_transport_fails_closed_when_the_last_step_finish_message_never_completed_text() -> (
    None
):
    session = "ses_lifecycle_unresolved"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_a",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {"id": "prt_2", "messageID": "msg_a", "type": "step-finish"},
            }
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_3",
                    "messageID": "msg_b",
                    "type": "text",
                    "text": "A second completed candidate.",
                    "time": {"start": 3, "end": 4},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                # The stream's last step_finish belongs to msg_c, which
                # never completed any text -- still genuinely ambiguous.
                "part": {"id": "prt_4", "messageID": "msg_c", "type": "step-finish"},
            }
        ),
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_multiple_terminal_candidates"


def test_decode_run_transport_fails_closed_on_a_single_candidate_superseded_by_later_lifecycle_activity() -> (
    None
):
    # Edge case closed alongside issue #85: a lone completed candidate is
    # not automatically terminal just because no *other* completed text
    # exists. If the stream's last step_finish belongs to a different,
    # still-textless message, that later step's own conclusion was never
    # accounted for, so trusting the earlier text as terminal would be a
    # guess, not a proof -- the pre-fix code returned it unconditionally
    # whenever `completed_order` held exactly one entry.
    session = "ses_lifecycle_single_candidate_superseded"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_a",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {"id": "prt_2", "messageID": "msg_a", "type": "step-finish"},
            }
        ),
        json.dumps(
            {
                "type": "step_start",
                "sessionID": session,
                "part": {"id": "prt_3", "messageID": "msg_b", "type": "step-start"},
            }
        ),
        json.dumps(
            {
                "type": "tool_use",
                "sessionID": session,
                "part": {
                    "id": "prt_4",
                    "messageID": "msg_b",
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "completed", "output": "still working"},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                # msg_b's step concludes last but msg_b never completed
                # text: msg_a's earlier completed text must not be trusted
                # just because it is the sole candidate.
                "part": {"id": "prt_5", "messageID": "msg_b", "type": "step-finish"},
            }
        ),
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_multiple_terminal_candidates"


def test_decode_run_transport_fails_closed_when_trailing_tool_activity_never_concludes() -> (
    None
):
    # Distinct edge case from the "superseded by a later step_finish" test
    # above: here the trailing activity after msg_a's step_finish never
    # gets *any* step_finish of its own -- the stream just ends mid-step.
    # last_step_finish_message_id would still (wrongly) point at msg_a
    # unless trailing activity is tracked independently of it, since
    # nothing ever overwrites that pointer. msg_a must not be trusted as
    # terminal: the stream never proved it was the last word.
    session = "ses_trailing_tool_activity_never_concludes"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_a",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {"id": "prt_2", "messageID": "msg_a", "type": "step-finish"},
            }
        ),
        json.dumps(
            {
                "type": "step_start",
                "sessionID": session,
                "part": {"id": "prt_3", "messageID": "msg_b", "type": "step-start"},
            }
        ),
        json.dumps(
            {
                "type": "tool_use",
                "sessionID": session,
                "part": {
                    "id": "prt_4",
                    "messageID": "msg_b",
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "completed", "output": "still working"},
                },
            }
        ),
        # Stream ends here -- no step_finish for msg_b at all.
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_multiple_terminal_candidates"


def test_decode_run_transport_fails_closed_when_trailing_incomplete_text_never_concludes() -> (
    None
):
    # Same gap, triggered by a trailing *text* part instead of a tool call:
    # an in-progress (no time.end) text update for a different message
    # after msg_a's step_finish, with the stream ending before that
    # message ever gets its own step_finish or completes any text.
    session = "ses_trailing_incomplete_text_never_concludes"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_a",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {"id": "prt_2", "messageID": "msg_a", "type": "step-finish"},
            }
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_3",
                    "messageID": "msg_b",
                    "type": "text",
                    "text": "Still drafting the actual response...",
                    "time": {"start": 3},  # no "end" -- still in progress
                },
            }
        ),
        # Stream ends here -- msg_b never completes and never gets a
        # step_finish either.
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_multiple_terminal_candidates"


def test_decode_run_transport_accepts_trailing_activity_that_concludes_with_its_own_step_finish() -> (
    None
):
    # Contrast case: trailing activity for a *different* message than the
    # earlier completed one is fine when it goes on to conclude with its
    # own step_finish and its own completed text -- this is the ordinary
    # multi-step shape (also covered by the
    # coder-intermediate-then-terminal-text.ndjson fixture) and must keep
    # working after the trailing-activity check above was added.
    session = "ses_trailing_activity_properly_concludes"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_a",
                    "type": "text",
                    "text": "Now scaffolding the requested change.",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {"id": "prt_2", "messageID": "msg_a", "type": "step-finish"},
            }
        ),
        json.dumps(
            {
                "type": "step_start",
                "sessionID": session,
                "part": {"id": "prt_3", "messageID": "msg_b", "type": "step-start"},
            }
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_4",
                    "messageID": "msg_b",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 3, "end": 4},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {"id": "prt_5", "messageID": "msg_b", "type": "step-finish"},
            }
        ),
    ]
    result = decode_run_transport("\n".join(lines))
    assert result.terminal_text == "AGENT_STATUS: COMPLETED"


def test_decode_run_transport_and_parse_agent_response_actually_reach_the_reviewer_phase() -> (
    None
):
    # Regression for issue #85: before the fix, this exact transcript raised
    # PROTOCOL_ERROR at decode_run_transport and never reached protocol
    # parsing at all, so the pipeline's own state machine never saw a
    # CODER_COMPLETED event and could never leave the CODER phase. This
    # drives the real, pure `state_machine.transition` with the same
    # agent_status -> TransitionEventKind mapping
    # `orchestrator._coder_event` applies (response.agent_status is
    # AgentStatus.COMPLETED -> TransitionEventKind.CODER_COMPLETED),
    # proving the pipeline actually advances to REVIEWER -- not just that
    # the marker parses.
    result = decode_run_transport(
        _text(RUN_FIXTURES / "coder-intermediate-then-terminal-text.ndjson")
    )
    locator = IssueLocator(
        RepositoryIdentity(
            host="github.com",
            owner="octocat",
            repository="hello-world",
            source="test",
        ),
        number=1,
    )
    parsed = parse_agent_response(
        AgentRole.CODER, result.terminal_text, issue_locator=locator
    )
    assert parsed.agent_status is AgentStatus.COMPLETED

    event = (
        TransitionEvent(kind=TransitionEventKind.CODER_COMPLETED)
        if parsed.agent_status is AgentStatus.COMPLETED
        else TransitionEvent(
            kind=TransitionEventKind.TERMINAL_OUTCOME,
            outcome=RunOutcome.PROTOCOL_ERROR,
        )
    )
    outcome = transition(
        PipelineState(phase=PipelinePhase.CODER, review_cycle=1),
        event,
        max_review_cycles=3,
    )
    assert outcome.state.phase is PipelinePhase.REVIEWER
    assert outcome.action is PipelineAction.INVOKE_REVIEWER


# --- issue #87: incomplete tool-call lifecycle and blank terminal text -------


def test_decode_run_transport_rejects_incomplete_tool_call_lifecycle_fixture() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport(
            _text(RUN_FIXTURES / "coder-incomplete-tool-call-lifecycle.ndjson")
        )
    assert exc_info.value.code == "opencode.transport_incomplete_tool_call_lifecycle"
    assert exc_info.value.code != "protocol.marker_missing"


def test_decode_run_transport_rejects_whitespace_only_completed_text() -> None:
    text = json.dumps(
        {
            "type": "text",
            "sessionID": "ses_blank_terminal_text",
            "part": {
                "id": "prt_1",
                "messageID": "msg_1",
                "type": "text",
                "text": " \t ",
                "time": {"start": 1, "end": 2},
            },
        }
    )
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport(text)
    assert exc_info.value.code == "opencode.transport_no_terminal_text"


def test_decode_run_transport_blank_last_write_invalidates_same_message_candidate() -> (
    None
):
    session = "ses_blank_last_write"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_2",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "   ",
                    "time": {"start": 3, "end": 4},
                },
            }
        ),
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_no_terminal_text"


def test_decode_run_transport_rejects_nonblank_text_when_final_reason_is_tool_calls() -> (
    None
):
    session = "ses_final_tool_calls"
    lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {
                    "id": "prt_2",
                    "messageID": "msg_1",
                    "type": "step-finish",
                    "reason": "tool-calls",
                },
            }
        ),
    ]
    with pytest.raises(ProtocolError) as exc_info:
        decode_run_transport("\n".join(lines))
    assert exc_info.value.code == "opencode.transport_incomplete_tool_call_lifecycle"


def test_decode_run_transport_accepts_intermediate_tool_calls_then_terminal_stop() -> (
    None
):
    session = "ses_tool_calls_then_stop"
    lines = [
        json.dumps(
            {
                "type": "step_start",
                "sessionID": session,
                "part": {
                    "id": "prt_1",
                    "messageID": "msg_a",
                    "type": "step-start",
                },
            }
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_2",
                    "messageID": "msg_a",
                    "type": "text",
                    "text": "Intermediate progress.",
                    "time": {"start": 1, "end": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {
                    "id": "prt_3",
                    "messageID": "msg_a",
                    "type": "step-finish",
                    "reason": "tool-calls",
                },
            }
        ),
        json.dumps(
            {
                "type": "step_start",
                "sessionID": session,
                "part": {
                    "id": "prt_4",
                    "messageID": "msg_b",
                    "type": "step-start",
                },
            }
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": session,
                "part": {
                    "id": "prt_5",
                    "messageID": "msg_b",
                    "type": "text",
                    "text": "AGENT_STATUS: COMPLETED",
                    "time": {"start": 3, "end": 4},
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {
                    "id": "prt_6",
                    "messageID": "msg_b",
                    "type": "step-finish",
                    "reason": "stop",
                },
            }
        ),
    ]
    result = decode_run_transport("\n".join(lines))
    assert result.terminal_text == "AGENT_STATUS: COMPLETED"


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

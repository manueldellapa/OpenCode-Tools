"""Static and cross-checked effective-policy tests for the three
project-local agent definitions (M11-04; FR-010, FR-011, FR-020-FR-023;
ADR-001, ADR-003, ADR-005, ADR-010).

Front-matter format note
-------------------------
`.opencode/agents/*.md` front matter between the `---` fences is written as
a plain JSON object rather than genuine YAML. JSON is a syntactic subset of
YAML for exactly the flat string/bool value plus one-level-nested-object
shape these three files need, and the standard library's `json` module then
parses it with zero bespoke parsing code -- this project is stdlib-only at
runtime (CLAUDE.md), and the standard library has no YAML parser of its
own. Keeping this convention identical across all three files means the
tiny loader below (`_load_agent_definition`) needs no per-file
special-casing; a hand-rolled key/value mini-parser was the other option
this milestone considered and rejected as strictly more code for the same
guarantee.

Scope note
----------
This module deliberately does not re-test `check_debug_agent` or
`check_debug_config`'s own pass/fail branches: `tests/unit/
test_opencode_adapter.py` already exercises every fixture in
`tests/fixtures/opencode/1.17.18/debug/` for both functions, positive and
negative. What this module proves instead is that the *static* file
content on disk -- fed through those same already-shipped, unmodified
functions -- is accepted (or, for one deliberately wrong synthetic input,
rejected), i.e. that these files are round-trip compatible with the real
effective-policy validator, not that the validator itself works.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final, cast

import pytest

from opencode_tools.domain import AgentRole
from opencode_tools.errors import PreflightError
from opencode_tools.opencode import (
    _PERMISSION_BASELINE,  # ground truth for permission, never hand-copied
    check_debug_agent,
    check_debug_config,
    compute_control_plane_digest,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
AGENTS_DIR: Final = REPO_ROOT / ".opencode" / "agents"
SRC_ROOT: Final = REPO_ROOT / "src" / "opencode_tools"

_ROLE_TOKENS: Final = ("architect", "coder", "reviewer")
_ROLE_BY_TOKEN: Final[dict[str, AgentRole]] = {
    "architect": AgentRole.ARCHITECT,
    "coder": AgentRole.CODER,
    "reviewer": AgentRole.REVIEWER,
}

_FRONT_MATTER_FENCE = "---\n"
_FRONT_MATTER_END = "\n---\n"


def _load_agent_definition(token: str) -> tuple[dict[str, object], str]:
    """Parse `.opencode/agents/<token>.md` into (front_matter, body)."""

    path = AGENTS_DIR / f"{token}.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith(_FRONT_MATTER_FENCE), (
        f"{path} must start with a '---' front-matter fence"
    )
    remainder = text[len(_FRONT_MATTER_FENCE) :]
    end_index = remainder.index(_FRONT_MATTER_END)
    front_matter_raw = remainder[:end_index]
    body = remainder[end_index + len(_FRONT_MATTER_END) :]

    parsed: object = json.loads(front_matter_raw)
    if not isinstance(parsed, dict):
        raise TypeError(f"{path} front matter must be a JSON object")
    return cast(dict[str, object], parsed), body


def _synthetic_debug_agent_response(token: str) -> dict[str, object]:
    """Build a dict shaped exactly like a real `opencode debug agent
    <role>` response, derived only from what was parsed out of that role's
    `.md` file -- never hand-typed.
    """

    front_matter, _ = _load_agent_definition(token)
    return {
        "name": token,
        "mode": front_matter["mode"],
        "tools": front_matter["tools"],
        "permission": front_matter["permission"],
    }


# =============================================================================
# File inventory: exactly three files, no fourth/orchestrator/subagent file
# =============================================================================


def test_exactly_three_agent_definition_files_exist() -> None:
    entries = sorted(p.name for p in AGENTS_DIR.iterdir())
    assert entries == ["architect.md", "coder.md", "reviewer.md"]


def test_no_orchestrator_or_subagent_definition_file_exists() -> None:
    stems = {p.stem.lower() for p in AGENTS_DIR.iterdir()}
    assert "orchestrator" not in stems
    assert "subagent" not in stems


# =============================================================================
# Per-role static shape: mode, tools, model, permission
# =============================================================================


@pytest.mark.parametrize("token", _ROLE_TOKENS)
def test_agent_definition_declares_mode_primary(token: str) -> None:
    front_matter, _ = _load_agent_definition(token)
    assert front_matter["mode"] == "primary"


@pytest.mark.parametrize("token", _ROLE_TOKENS)
def test_agent_definition_disables_ask_and_task(token: str) -> None:
    front_matter, _ = _load_agent_definition(token)
    assert front_matter["tools"] == {"ask": False, "task": False}


@pytest.mark.parametrize("token", _ROLE_TOKENS)
def test_agent_definition_declares_a_non_empty_model(token: str) -> None:
    front_matter, _ = _load_agent_definition(token)
    model = front_matter["model"]
    assert isinstance(model, str)
    assert model.strip() != ""


@pytest.mark.parametrize("token", _ROLE_TOKENS)
def test_agent_definition_permission_matches_the_reviewed_baseline(
    token: str,
) -> None:
    """Permission must match `opencode.py`'s own `_PERMISSION_BASELINE`
    exactly, imported directly here so the two cannot silently drift apart.
    """

    role = _ROLE_BY_TOKEN[token]
    front_matter, _ = _load_agent_definition(token)
    assert front_matter["permission"] == _PERMISSION_BASELINE[role]


# =============================================================================
# Body content: architect/reviewer read-only, reviewer's given-context-only
# scope, coder's write scope and FR-021 forbidden-action inventory
# =============================================================================


@pytest.mark.parametrize("token", ["architect", "reviewer"])
def test_architect_and_reviewer_bodies_state_they_are_read_only(token: str) -> None:
    _, body = _load_agent_definition(token)
    lowered = body.lower()
    assert "read-only" in lowered or "read only" in lowered
    assert "must not edit" in lowered


def test_reviewer_body_states_it_never_fetches_anything_itself() -> None:
    _, body = _load_agent_definition("reviewer")
    lowered = body.lower()
    assert "must not fetch" in lowered


@pytest.mark.parametrize(
    "expected_substring",
    ["issue", "handoff", "report", "change inventory", "test-scope"],
)
def test_reviewer_body_names_each_prompt_supplied_input_it_relies_on(
    expected_substring: str,
) -> None:
    _, body = _load_agent_definition("reviewer")
    assert expected_substring in body.lower()


def test_coder_body_states_its_only_write_scope_is_the_target_working_tree() -> None:
    _, body = _load_agent_definition("coder")
    lowered = body.lower()
    assert "write scope" in lowered
    assert "target working tree" in lowered


def test_coder_body_states_everything_must_stay_uncommitted() -> None:
    _, body = _load_agent_definition("coder")
    assert "uncommitted" in body.lower()


# One assertion per FR-021 forbidden-action item, each with its own failure
# message, so a future regression names exactly which item went missing
# instead of failing one giant regex.


def test_coder_body_forbids_staging() -> None:
    _, body = _load_agent_definition("coder")
    assert "git add" in body.lower(), "coder.md must forbid staging (git add)"


def test_coder_body_forbids_commit() -> None:
    _, body = _load_agent_definition("coder")
    assert "commit" in body.lower(), "coder.md must forbid commit"


def test_coder_body_forbids_amend() -> None:
    _, body = _load_agent_definition("coder")
    assert "amend" in body.lower(), "coder.md must forbid amending a commit"


def test_coder_body_forbids_tag() -> None:
    _, body = _load_agent_definition("coder")
    assert "tag" in body.lower(), "coder.md must forbid tag creation"


def test_coder_body_forbids_branch_creation() -> None:
    _, body = _load_agent_definition("coder")
    assert "branch" in body.lower(), "coder.md must forbid branch creation"


def test_coder_body_forbids_push() -> None:
    _, body = _load_agent_definition("coder")
    assert "push" in body.lower(), "coder.md must forbid push"


def test_coder_body_forbids_force_push() -> None:
    _, body = _load_agent_definition("coder")
    assert "force" in body.lower(), "coder.md must forbid force-push"


def test_coder_body_forbids_merge() -> None:
    _, body = _load_agent_definition("coder")
    assert "merge" in body.lower(), "coder.md must forbid merge"


def test_coder_body_forbids_rebase() -> None:
    _, body = _load_agent_definition("coder")
    assert "rebase" in body.lower(), "coder.md must forbid rebase"


def test_coder_body_forbids_reset() -> None:
    _, body = _load_agent_definition("coder")
    assert "reset" in body.lower(), "coder.md must forbid reset"


def test_coder_body_forbids_clean() -> None:
    _, body = _load_agent_definition("coder")
    assert "clean" in body.lower(), "coder.md must forbid clean"


def test_coder_body_forbids_stash() -> None:
    _, body = _load_agent_definition("coder")
    assert "stash" in body.lower(), "coder.md must forbid stash"


def test_coder_body_forbids_destructive_checkout_or_switch() -> None:
    lowered = _load_agent_definition("coder")[1].lower()
    assert "checkout" in lowered, "coder.md must forbid destructive checkout"
    assert "switch" in lowered, "coder.md must forbid destructive switch"


def test_coder_body_forbids_pr_mutation() -> None:
    _, body = _load_agent_definition("coder")
    assert "gh pr" in body.lower(), "coder.md must forbid PR mutation"


def test_coder_body_forbids_issue_mutation() -> None:
    _, body = _load_agent_definition("coder")
    assert "gh issue" in body.lower(), "coder.md must forbid issue mutation"


# =============================================================================
# Compatibility with the real, already-shipped `debug agent` / `debug
# config` effective-policy validators (ADR-005 SS10.4)
# =============================================================================


@pytest.mark.parametrize("token", _ROLE_TOKENS)
def test_agent_definition_round_trips_through_the_real_debug_agent_check(
    token: str,
) -> None:
    role = _ROLE_BY_TOKEN[token]
    synthetic = _synthetic_debug_agent_response(token)

    check_debug_agent(role, synthetic)  # must not raise


def test_a_more_permissive_synthetic_permission_than_declared_is_rejected() -> None:
    """Proves the round-trip is a real check, not a tautology: mutating the
    architect's own parsed permission to be more permissive than declared
    must still fail the real validator (M11-04 AC: "effective policy più
    permissiva fallisce").
    """

    synthetic = _synthetic_debug_agent_response("architect")
    synthetic["permission"] = {"edit": "allow", "bash": "deny", "webfetch": "deny"}

    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, synthetic)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


def test_a_config_baseline_shaped_synthetic_dict_passes_check_debug_config() -> None:
    synthetic_config: dict[str, object] = {
        "share": "manual",
        "autoshare": False,
        "autoupdate": False,
        "agent": {token: {"mode": "primary"} for token in _ROLE_TOKENS},
    }

    check_debug_config(synthetic_config)  # must not raise


def test_a_synthetic_dict_with_auto_share_is_rejected_by_check_debug_config() -> None:
    """Proves the previous test actually exercises real logic rather than a
    tautology: flipping only `share` to `"auto"` must still raise.
    """

    synthetic_config: dict[str, object] = {
        "share": "auto",
        "autoshare": False,
        "autoupdate": False,
        "agent": {token: {"mode": "primary"} for token in _ROLE_TOKENS},
    }

    with pytest.raises(PreflightError) as exc_info:
        check_debug_config(synthetic_config)
    assert exc_info.value.code == "opencode.debug_config_rejected"


def test_control_plane_digest_computes_over_the_real_agent_definitions() -> None:
    """M11-04's own "Test richiesti" line names the control-plane digest
    explicitly. `tests/unit/test_opencode_adapter.py` already proves
    `compute_control_plane_digest`'s own behavior (key-order stability,
    content sensitivity) against static fixture JSON; what this test proves
    instead is that the function runs end-to-end over evidence shaped from
    the real, disk-resident `.opencode/agents/*.md` files, not only over
    fixtures that merely resemble them.
    """

    agents = {
        role: _synthetic_debug_agent_response(token)
        for token, role in _ROLE_BY_TOKEN.items()
    }
    config: dict[str, object] = {
        "share": "manual",
        "autoshare": False,
        "autoupdate": False,
    }

    digest = compute_control_plane_digest(config=config, agents=agents)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)


# =============================================================================
# Absence of model/provider ID literals from the Python package (FR-020's
# "modelli solo nei file agent e sostituibili fra run")
# =============================================================================


def test_no_agent_model_id_literal_appears_anywhere_in_the_python_package() -> None:
    model_ids = {
        str(_load_agent_definition(token)[0]["model"]) for token in _ROLE_TOKENS
    }
    assert model_ids, "sanity: at least one model id must have been parsed"

    for path in sorted(SRC_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for model_id in model_ids:
            assert model_id not in text, (
                f"{path} must not hardcode the agent model id {model_id!r}; "
                "models belong only in .opencode/agents/*.md (FR-020)"
            )


def test_config_module_defines_no_model_related_default_constant() -> None:
    config_text = (SRC_ROOT / "config.py").read_text(encoding="utf-8")
    default_constant_names = re.findall(
        r"^(_DEFAULT_[A-Z0-9_]+)\s*=", config_text, flags=re.MULTILINE
    )
    assert default_constant_names, "sanity: config.py must define _DEFAULT_* constants"
    assert not any("MODEL" in name for name in default_constant_names)

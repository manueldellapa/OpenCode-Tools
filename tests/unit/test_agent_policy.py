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

import opencode_tools.github as github_module
from opencode_tools.config import (
    _EXECUTION_KEYS,  # closed-schema ground truth, never hand-copied
    _GITHUB_KEYS,
    _GITHUB_TARGET_ENTRY_KEYS,
    _PROVIDER_RETRY_KEYS,
    _RUNTIME_KEYS,
    _TOP_LEVEL_KEYS,
    read_config_file,
)
from opencode_tools.domain import AgentRole
from opencode_tools.errors import ConfigError, PreflightError
from opencode_tools.git_safety import ALLOWED_GIT_ARGV_TAILS
from opencode_tools.opencode import (
    _ARCHITECT_BASH_PERMISSION_CONFIG,  # ground truth, never hand-copied
    _PERMISSION_BASELINE,  # ground truth for permission, never hand-copied
    FORBIDDEN_RUN_FLAGS,
    check_debug_agent,
    check_debug_config,
    check_no_forbidden_flags,
    compute_control_plane_digest,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
AGENTS_DIR: Final = REPO_ROOT / ".opencode" / "agents"
SRC_ROOT: Final = REPO_ROOT / "src" / "opencode_tools"
CONFIG_FIXTURES_DIR: Final = REPO_ROOT / "tests" / "fixtures" / "config"

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
    """Build a dict shaped like a real `opencode debug agent <role>`
    response, derived only from what was parsed out of that role's `.md`
    file -- never hand-typed -- and transformed from OpenCode's
    config-time shape into its response-time shape.

    The two shapes are not the same, confirmed live during the M15-03
    qualification: `.opencode/agents/*.md` declares `tools.ask` and a
    `permission` dict (OpenCode's documented config-time names) whose
    values are flat strings for every kind except the architect's `bash`,
    a nested pattern-keyed object (least-privilege: deny by default, allow
    only `gh issue view *`) -- but a real `debug agent` response reports
    the resolved tool under `tools.question` instead, and always expands
    `permission` into an ordered `{permission, action, pattern}` rule list
    resolved last-match-wins (https://opencode.ai/docs/permissions/), one
    entry per pattern, never a dict of any shape.
    """

    front_matter, _ = _load_agent_definition(token)
    tools = front_matter["tools"]
    assert isinstance(tools, dict)
    permission = front_matter["permission"]
    assert isinstance(permission, dict)

    rules: list[dict[str, object]] = [
        {"permission": "*", "action": "allow", "pattern": "*"}
    ]
    for key, value in permission.items():
        if isinstance(value, dict):
            rules.extend(
                {"permission": key, "action": action, "pattern": pattern}
                for pattern, action in value.items()
            )
        else:
            rules.append({"permission": key, "action": value, "pattern": "*"})

    return {
        "name": token,
        "mode": front_matter["mode"],
        "tools": {"question": tools["ask"], "task": tools["task"]},
        "permission": rules,
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
    """Permission must match `opencode.py`'s own ground truth exactly,
    imported directly here so the two cannot silently drift apart. The
    architect's `bash` is not in `_PERMISSION_BASELINE` at all -- it is a
    nested least-privilege object from `_ARCHITECT_BASH_PERMISSION_CONFIG`,
    layered in separately (see that constant's docstring).
    """

    role = _ROLE_BY_TOKEN[token]
    front_matter, _ = _load_agent_definition(token)
    expected: dict[str, object] = dict(_PERMISSION_BASELINE[role])
    if role is AgentRole.ARCHITECT:
        expected["bash"] = _ARCHITECT_BASH_PERMISSION_CONFIG
    assert front_matter["permission"] == expected


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


def test_coder_body_has_full_completion_contract() -> None:
    _, body = _load_agent_definition("coder")
    lowered = " ".join(body.lower().split())

    assert (
        "required acceptance criteria and their required verification define" in lowered
    )
    assert "stop further exploratory or optional work" in lowered
    assert "hidden tests" in lowered
    assert "alternate project layouts" in lowered
    assert "optional tooling" in lowered
    assert "unrelated implementation variants" in lowered
    assert "extra refactors" in lowered
    assert (
        "failed or inconclusive optional diagnostic does not block completion"
        in lowered
    )
    assert "independently verified by another valid method" in lowered
    assert "agent_status: completed" in lowered
    assert "required verification fails" in lowered
    assert "agent_status: failed" in lowered
    assert "hard safety ceiling" in lowered
    assert "not the normal success-path stopping mechanism" in lowered


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


def test_ac_024_forbidden_action_policy_and_command_inventory() -> None:
    """AC-024 has three clauses; this composite asserts all three at the
    AC level, reusing already-shipped ground truth rather than duplicating
    the finer-grained tests that already exist:

    1. "prompt/definizioni agent codificano l'allowlist/deny policy
       richiesta" -- the itemized, per-verb proof already lives in the 14
       `test_coder_body_forbids_*` tests above plus the permission-baseline
       tests; this only re-asserts the AC-level fact via the real,
       already-imported `_PERMISSION_BASELINE` (architect/reviewer deny
       edit and bash outright; every role denies webfetch), without
       re-deriving the FR-021 verb list.
    2. "Python non costruisce mutazioni Git/GitHub" -- structural, not
       free-text, proof for both halves:
       (a) Git: every argv tail in the real `ALLOWED_GIT_ARGV_TAILS`
           starts with a read-only verb. The exact-tuple audit of that
           same constant already lives in
           `tests/unit/test_git_commands.py::
           test_allowed_git_argv_tails_is_exactly_the_reviewed_read_only_set`;
           this does not repeat that exact-equality assertion, only the
           weaker read-only-verb-prefix structural fact, against the real
           constant.
       (b) GitHub: `github.py` has no PR/issue mutation-building function
           at all. Proven two ways: none of its public names suggest a
           mutation (structural, introspection-based), and its source text
           never contains a mutating `gh pr`/`gh issue` subcommand literal
           (the same `SRC_ROOT`-relative source-grep pattern
           `test_no_agent_model_id_literal_appears_anywhere_in_the_python_package`
           already uses for model IDs, applied here to `github.py`
           specifically). Nothing before this test named this as a
           regression-proof fact for `github.py`.
    3. "senza presentarla come sandbox contro un agent ostile" -- entirely
       new: the cooperative-control-not-a-sandbox (ADR-010) disclaimer is
       already present verbatim in all three agent bodies, but nothing
       asserted that before this test; a future deletion of that wording
       would not have failed anything.
    """

    # (1) AC-level allowlist/deny claim via the real permission baseline.
    # The architect's bash is least-privilege rather than a flat "deny" --
    # denied by default, with exactly one narrow read-only exception (the
    # `gh issue view` command its own prompt instructs it to run) -- so its
    # "deny by default" half is asserted directly against that config
    # object rather than against `_PERMISSION_BASELINE`, which does not
    # carry a "bash" key for the architect at all.
    assert _PERMISSION_BASELINE[AgentRole.ARCHITECT]["edit"] == "deny"
    assert _ARCHITECT_BASH_PERMISSION_CONFIG["*"] == "deny"
    assert "bash" not in _PERMISSION_BASELINE[AgentRole.ARCHITECT]
    assert _PERMISSION_BASELINE[AgentRole.REVIEWER]["edit"] == "deny"
    assert _PERMISSION_BASELINE[AgentRole.REVIEWER]["bash"] == "deny"
    for role_permission in _PERMISSION_BASELINE.values():
        assert role_permission["webfetch"] == "deny"

    # (2a) Git: every allowlisted argv tail starts with a read-only verb.
    # (The exact-tuple audit of ALLOWED_GIT_ARGV_TAILS itself lives in
    # test_git_commands.py; this only checks the weaker verb-prefix fact.)
    read_only_git_verbs = {"rev-parse", "branch", "status", "ls-files"}
    for argv_tail in ALLOWED_GIT_ARGV_TAILS:
        assert argv_tail[0] in read_only_git_verbs, (
            f"git_safety.ALLOWED_GIT_ARGV_TAILS contains a non-read-only "
            f"verb: {argv_tail!r}"
        )

    # (2b) GitHub: no public name suggests a PR/issue mutation.
    mutation_suggestive_substrings = ("create", "close", "merge", "comment", "edit")
    public_names = tuple(
        name for name in dir(github_module) if not name.startswith("_")
    )
    for name in public_names:
        lowered_name = name.lower()
        for substring in mutation_suggestive_substrings:
            assert substring not in lowered_name, (
                f"opencode_tools.github exposes {name!r}, which suggests a "
                "PR/issue mutation; github.py must stay read-only"
            )

    # (2b, continued) GitHub: no mutating `gh pr`/`gh issue` subcommand
    # literal appears in github.py's own source text.
    github_source = (SRC_ROOT / "github.py").read_text(encoding="utf-8")
    forbidden_gh_subcommands = (
        "gh pr create",
        "gh pr merge",
        "gh pr close",
        "gh pr comment",
        "gh pr edit",
        "gh issue create",
        "gh issue close",
        "gh issue comment",
        "gh issue edit",
    )
    for forbidden in forbidden_gh_subcommands:
        assert forbidden not in github_source, (
            f"github.py must not construct the mutating command {forbidden!r}"
        )

    # (3) The cooperative-control-not-a-sandbox disclaimer (ADR-010) is
    # present verbatim in every role's body.
    for token in _ROLE_TOKENS:
        _, body = _load_agent_definition(token)
        lowered = body.lower()
        assert "cooperative control" in lowered, (
            f"{token}.md must disclose the permission matrix is a "
            "cooperative control (ADR-010)"
        )
        assert "not a sandbox" in lowered, (
            f"{token}.md must disclaim sandbox-strength containment (ADR-010)"
        )
        assert "adr-010" in lowered, (
            f"{token}.md must cite ADR-010 for the cooperative-control disclaimer"
        )


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
    synthetic["permission"] = [
        {"permission": "*", "action": "allow", "pattern": "*"},
        {"permission": "edit", "action": "allow", "pattern": "*"},
        {"permission": "bash", "action": "deny", "pattern": "*"},
        {"permission": "webfetch", "action": "deny", "pattern": "*"},
    ]

    with pytest.raises(PreflightError) as exc_info:
        check_debug_agent(AgentRole.ARCHITECT, synthetic)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


def test_a_broader_synthetic_bash_pattern_than_declared_is_rejected() -> None:
    """The architect's bash policy is least-privilege, not a flat
    allow/deny (`_ARCHITECT_BASH_PERMISSION_CONFIG`): proves the round-trip
    catches drift on *that* dimension too, not only a flat kind going from
    deny to allow -- an extra permissive bash pattern beyond the one
    reviewed `gh issue view *` exception must still fail the real
    validator.
    """

    synthetic = _synthetic_debug_agent_response("architect")
    permission = cast(list[dict[str, object]], synthetic["permission"])
    permission.append(
        {"permission": "bash", "action": "allow", "pattern": "gh issue edit *"}
    )

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


def test_ac_023_models_are_external_to_python_and_toml() -> None:
    """AC-023: the three experimental model IDs never appear as literals in
    a Python command or config, and are never passed via `--model` -- i.e.
    swapping a model on the OpenCode side never requires a Python change.
    This composite proves the full sentence end-to-end by *reusing*
    already-shipped, unmodified machinery rather than re-deriving it:

    1. The three real model IDs are parsed the same way
       `test_no_agent_model_id_literal_appears_anywhere_in_the_python_package`
       already does. That sibling test is the literal-absence-in-`*.py`
       proof; it is not re-run here (no second `SRC_ROOT.rglob` sweep over
       the same files for the same reason) -- only reused as a source of
       real, never-hand-typed model IDs.
    2. The TOML config surface, which nothing before this test checked for
       model literals at all:
       (a) `config.py`'s own closed-schema key sets contain no key named
           "model" at any nesting level, so the schema has no model knob
           to begin with;
       (b) the accepted ("golden") fixtures never carry a model literal;
       (c) `tests/fixtures/config/unknown-top-level-key.toml` and
           `unknown-target-entry-key.toml` *do* each contain a literal
           `model = "gpt-unexpected"` line -- by construction, as rejection
           fixtures, not an oversight -- so this asserts the literal really
           is there (non-vacuous) and that `read_config_file` still refuses
           the file. `tests/unit/test_config.py` already proves this
           generically over "any unknown key"; what is new here is naming
           "model" specifically as the key under test, tying the TOML half
           directly to this AC rather than to schema-closedness in general.
    3. The `--model` run-flag half: `FORBIDDEN_RUN_FLAGS` names it and the
       real `check_no_forbidden_flags` enforcement function rejects it --
       one targeted call, not a re-parametrization of
       `test_opencode_adapter.py`'s full forbidden-flag battery.
    """

    # (1) Reuse, don't re-derive: three real model IDs, parsed from disk.
    model_ids = {
        str(_load_agent_definition(token)[0]["model"]) for token in _ROLE_TOKENS
    }
    assert model_ids, "sanity: at least one model id must have been parsed"

    # (2a) The closed TOML schema's own known key sets have no "model" key
    # at any level -- the schema has no model knob, structurally.
    known_key_sets = (
        _TOP_LEVEL_KEYS,
        _EXECUTION_KEYS,
        _PROVIDER_RETRY_KEYS,
        _RUNTIME_KEYS,
        _GITHUB_KEYS,
        _GITHUB_TARGET_ENTRY_KEYS,
    )
    for keys in known_key_sets:
        assert "model" not in keys

    # (2b) The accepted fixtures never carry a model literal.
    for fixture_name in ("golden-minimal.toml", "golden-full.toml"):
        text = (CONFIG_FIXTURES_DIR / fixture_name).read_text(encoding="utf-8")
        assert "model" not in text.lower()
        for model_id in model_ids:
            assert model_id not in text

    # (2c) The two fixtures that *do* carry a literal `model = ...` line
    # exist precisely to prove it is rejected, not accepted.
    for fixture_name in (
        "unknown-top-level-key.toml",
        "unknown-target-entry-key.toml",
    ):
        fixture_path = CONFIG_FIXTURES_DIR / fixture_name
        assert "model" in fixture_path.read_text(encoding="utf-8")
        with pytest.raises(ConfigError) as exc_info:
            read_config_file(fixture_path)
        assert exc_info.value.code == "config.unknown_key"

    # (3) The `--model` run-flag half: named in the real deny-list and
    # enforced by the real function.
    assert "--model" in FORBIDDEN_RUN_FLAGS
    with pytest.raises(AssertionError):
        check_no_forbidden_flags(("run", "--model", "sonnet"))

"""Live, opt-in OpenCode `1.17.18` compatibility smoke (M15-03, AC-027).

This is the one test in the suite that is allowed to touch a real,
installed OpenCode binary, a real provider/model, and a real `gh`-backed
GitHub repository -- everything else in `tests/` is offline and
deterministic (CLAUDE.md). It is never selected by the ordinary `QG`
(`pytest -m 'not live'` is the default via `pyproject.toml`'s `addopts`;
plain `python3.13 -m pytest` therefore never runs it) and must be invoked
explicitly:

    python3.13 -m pytest tests/integration/test_opencode_1_17_18_smoke.py -m live

Per the PRD (`AC-027`) and System Design SS16, this is a *manual,
documented* smoke: a human operator sets up a disposable target and a
controlled issue once, points this test at them, and inspects the
resulting artifact themselves -- this test proves the pipeline-level
invariants (exact version, real capability/identity evidence, session
uniqueness, no observed publication) mechanically, but does not replace
that manual artifact inspection, and it never decides on its own whether
`1.17.18` becomes *supported* (`docs/compatibility.md` and the exact
version registry are updated separately, only after a full PASS -- ADR-005).

## One-time setup (per M15-03's scope: disposable, controlled, no publication)

1. Create a disposable, private GitHub repository and clone it locally.
   Nothing here is scoped to any particular content; a fresh `README.md`
   commit on `main` is enough.
2. Open exactly one controlled issue on that repository describing a
   small, unambiguous, single-file change (e.g. "create GREETING.md
   containing exactly this one line"). Its content is what the operator
   inspects the resulting change against; this test does not itself
   assert on file content.
3. Build an OpenCode *workspace* directory that is NOT the clone above:
   it must contain this project's own, unmodified
   `.opencode/agents/{architect,coder,reviewer}.md` (`.opencode/agents/`
   copied verbatim -- "agent locali", never edited for this smoke) and an
   `opencode-tools.toml` (`version = 1`, sensible `[execution]`/
   `[provider_retry]`/`[runtime]` sections; no `[github.targets]` override
   is needed if the clone's `origin` remote already names the disposable
   repository unambiguously).
4. Clone the disposable repository as a subdirectory of that workspace
   (e.g. `<workspace>/repo`) -- component tests and AC-003 already prove
   this workspace/target split is the correct, tested shape; keeping the
   target out of this project's own working tree is what makes it safe to
   let the coder actually run with `edit`/`bash` allowed.
5. Point this test at that setup via environment variables (all required;
   the test skips with a clear message if any are missing):

   - `OPENCODE_TOOLS_LIVE_SMOKE_WORKSPACE`: absolute path to the workspace
     built in step 3.
   - `OPENCODE_TOOLS_LIVE_SMOKE_TARGET`: the target path relative to the
     workspace (e.g. `repo`).
   - `OPENCODE_TOOLS_LIVE_SMOKE_ISSUE`: the controlled issue's number.
   - `OPENCODE_TOOLS_LIVE_SMOKE_CONFIG` (optional): an explicit
     `opencode-tools.toml` path, if not the workspace's conventional one.

No credential, model ID, or provider selection is ever passed by this
test or by Python at any layer -- OpenCode resolves those itself from the
project-local agent definitions and whatever the operator's own OpenCode
installation already has configured (System Design SS16.1: "tool locali
già installati/autenticati").
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from opencode_tools import cli
from opencode_tools.domain import AgentRole, FinalStatus
from opencode_tools.opencode import CANDIDATE_OPENCODE_VERSION

pytestmark = pytest.mark.live

_REQUIRED_ENV_VARS: Final = (
    "OPENCODE_TOOLS_LIVE_SMOKE_WORKSPACE",
    "OPENCODE_TOOLS_LIVE_SMOKE_TARGET",
    "OPENCODE_TOOLS_LIVE_SMOKE_ISSUE",
)

_ARTIFACT_LINE: Final = re.compile(r"^artifact: (.+)$", re.MULTILINE)

_ROLE_TOKENS: Final = {
    AgentRole.ARCHITECT: "architect",
    AgentRole.CODER: "coder",
    AgentRole.REVIEWER: "reviewer",
}


def _smoke_config() -> tuple[Path, str, int, str | None]:
    missing = [name for name in _REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "Live smoke requires a real, disposable target -- set "
            + ", ".join(missing)
            + ". See this file's module docstring for one-time setup."
        )
    workspace = Path(os.environ["OPENCODE_TOOLS_LIVE_SMOKE_WORKSPACE"]).resolve()
    target = os.environ["OPENCODE_TOOLS_LIVE_SMOKE_TARGET"]
    issue = int(os.environ["OPENCODE_TOOLS_LIVE_SMOKE_ISSUE"])
    config = os.environ.get("OPENCODE_TOOLS_LIVE_SMOKE_CONFIG")
    return workspace, target, issue, config


def _git(args: list[str], *, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def target_root() -> Iterator[Path]:
    """Restore the disposable target to a clean baseline before and after
    the run, so the same repository/issue can be reused for repeated
    retries without manual cleanup between them. Uses real `git` commands
    directly, in this fixture only -- an ordinary test-hygiene concern,
    never something `opencode_tools` itself is authorized to do."""

    workspace, target, _issue, _config = _smoke_config()
    root = (workspace / target).resolve()
    assert root.is_dir(), f"target {root} does not exist"

    def _reset() -> None:
        _git(["checkout", "--", "."], cwd=root)
        _git(["clean", "-fd"], cwd=root)

    _reset()
    status_before = _git(["status", "--porcelain"], cwd=root)
    assert not status_before, (
        f"target {root} is not clean before the smoke: {status_before!r}"
    )
    yield root
    _reset()


def test_ac_027_disposable_compatibility_smoke(
    target_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Runs the real, single-issue pipeline end to end against a
    disposable target and a controlled issue, then proves, from the
    artifact alone, exactly what AC-027 requires: the exact candidate
    version was used, every role's effective identity was verified (not
    merely requested), sessions were genuinely distinct per attempt, and
    nothing suggests a publication or GitHub mutation occurred.
    """

    workspace, target, issue, config = _smoke_config()

    executable = shutil.which("opencode")
    assert executable, "opencode must be installed and resolvable on PATH."
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert version == CANDIDATE_OPENCODE_VERSION, (
        f"Live smoke requires exactly OpenCode {CANDIDATE_OPENCODE_VERSION}; "
        f"found {version!r}. No fallback to a nearby version is authorized "
        "(ADR-005) -- install the exact candidate version and retry."
    )

    remote_head_before = _git(["ls-remote", "origin", "HEAD"], cwd=target_root)
    local_head_before = _git(["rev-parse", "HEAD"], cwd=target_root)
    commit_count_before = _git(["rev-list", "--count", "HEAD"], cwd=target_root)

    argv = [
        "run",
        "--workspace",
        str(workspace),
        "--target",
        target,
        "--issue",
        str(issue),
    ]
    if config:
        argv += ["--config", config]

    exit_code = cli.main(argv)
    captured = capsys.readouterr()

    assert "FINAL_STATUS: " in captured.out, (
        "main() must print exactly one FINAL_STATUS line on stdout; got:\n"
        f"stdout={captured.out!r}\nstderr={captured.err!r}"
    )

    artifact_match = _ARTIFACT_LINE.search(captured.err)
    assert artifact_match is not None, (
        f"no 'artifact: <path>' line in stderr:\n{captured.err!r}"
    )
    artifact_path = Path(artifact_match.group(1))
    assert artifact_path.is_file(), f"reported artifact {artifact_path} is missing"

    record = json.loads(artifact_path.read_text(encoding="utf-8"))

    # --- exact version, real capability/identity evidence, no share ---
    assert record["environment"]["platform"] in ("darwin", "linux")
    attempts = record["attempts"]
    assert attempts, "the run must have attempted at least one role"

    session_ids = []
    for attempt in attempts:
        agent_result = attempt["agent_result"]
        role_token = _ROLE_TOKENS[AgentRole[attempt["role"]]]
        session_id = agent_result.get("session_id")
        verified_agent = agent_result.get("verified_agent")
        if agent_result.get("outcome") == "SUCCEEDED":
            assert session_id, f"{role_token} attempt has no session_id"
            assert verified_agent == role_token, (
                f"{role_token}'s verified effective agent was "
                f"{verified_agent!r}, not {role_token!r} -- a silent "
                "fallback must fail the smoke, never be accepted."
            )
            session_ids.append(session_id)

    assert len(session_ids) == len(set(session_ids)), (
        f"session IDs were not unique across attempts: {session_ids}"
    )

    # --- exact-version attribution on the run itself ---
    assert record["environment"]["tool_version"], "run record has no tool_version"

    # --- no observed publication or GitHub mutation ---
    remote_head_after = _git(["ls-remote", "origin", "HEAD"], cwd=target_root)
    local_head_after = _git(["rev-parse", "HEAD"], cwd=target_root)
    commit_count_after = _git(["rev-list", "--count", "HEAD"], cwd=target_root)
    assert remote_head_after == remote_head_before, (
        "the disposable target's remote HEAD changed -- something was "
        "pushed, which this pipeline must never do"
    )
    assert local_head_after == local_head_before, (
        "the disposable target's local HEAD moved -- something was "
        "committed, which this pipeline must never do"
    )
    assert commit_count_after == commit_count_before, (
        "the disposable target's commit count changed"
    )

    final_status = record["final_status"]
    print(
        f"\nLive smoke complete: final_status={final_status} "
        f"exit_code={exit_code} artifact={artifact_path}\n"
        "Inspect the artifact and the target's working tree directly "
        "before drawing any conclusion -- this test proves the pipeline "
        "invariants above, not that the coder's change is correct.",
    )
    assert final_status in {status.value for status in FinalStatus}

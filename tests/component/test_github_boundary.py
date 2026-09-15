"""Component tests for `resolve_repository_identity`'s real Git wiring
(M11-01; ADR-007; System Design SS17.1) and the `gh` preflight / issue
locator boundary (M11-02; ADR-007; ADR-010; System Design SS17.2/SS17.3).

Builds real temporary Git repositories, adds real remotes via `git remote
add`, and drives `resolve_repository_identity` through the real
`SubprocessRunner` and a real `git` executable -- mirrors
`tests/component/test_git_repository.py`'s setup style. This file proves
real-process wiring for a handful of representative scenarios only; the
exhaustive precedence/ambiguity matrix is unit-tested directly against
in-memory `RemoteFetchUrl` data in `tests/unit/test_github_identity.py`.

The `gh` preflight tests below spawn `tests/component/helpers/fake_gh.py`
through that same real `SubprocessRunner`, exactly like
`tests/component/test_opencode_preflight.py` spawns `fake_opencode.py`, to
prove `run_gh_preflight`/`locate_issue` build the real two-call sequence
System Design SS17.2 fixes and fail closed on every scenario it requires
evidence for -- a missing `gh` executable, an unparsable version, an auth
failure (a clean non-zero exit and a timeout), and, throughout, that
`gh auth status`'s raw stdout/stderr never reaches a raised
`PreflightError`'s own fields.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import (
    AgentRole,
    AgentStatus,
    GithubTargetOverride,
    IssueLocator,
    IssueRef,
    RepositoryIdentity,
    Workspace,
)
from opencode_tools.errors import PreflightError, ProtocolError
from opencode_tools.git_safety import resolve_git_executable, resolve_target
from opencode_tools.github import (
    GhPreflightEvidence,
    locate_issue,
    resolve_gh_executable,
    resolve_repository_identity,
    run_gh_preflight,
)
from opencode_tools.process import SubprocessRunner
from opencode_tools.prompting import build_architect_prompt, build_coder_prompt
from opencode_tools.protocol import parse_agent_response

UTILITY_TIMEOUT_SECONDS = 5.0
TERMINATION_GRACE_SECONDS = 1.0

GIT_EXECUTABLE = resolve_git_executable()
FAKE_GH = Path(__file__).resolve().parent / "helpers" / "fake_gh.py"
AGENT_DEFINITIONS_DIR = Path(__file__).resolve().parents[2] / ".opencode" / "agents"

# A short bound for the deliberate-timeout scenario, so that test stays
# fast; `FAKE_GH_AUTH_SLEEP_SECONDS` is set well above this in that test.
SHORT_TIMEOUT_SECONDS = 0.3
SHORT_GRACE_SECONDS = 0.2


class RealClock:
    """A `Clock` reading genuine wall/monotonic time for a real subprocess."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / "file.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "initial",
        ],
        cwd=root,
    )
    return root


def _add_remote(root: Path, name: str, url: str) -> None:
    _git(["remote", "add", name, url], cwd=root)


def _workspace(root: Path) -> Workspace:
    root.mkdir(parents=True, exist_ok=True)
    return Workspace(root=root.resolve())


def _resolve_identity(
    workspace: Workspace,
    repo_root: Path,
    *,
    github_targets: tuple[GithubTargetOverride, ...] = (),
) -> RepositoryIdentity:
    target = resolve_target(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        workspace=workspace,
        target_root=repo_root,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )
    return resolve_repository_identity(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        target=target,
        github_targets=github_targets,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


def test_a_repository_with_a_valid_origin_resolves_from_origin(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _init_repo(workspace.root / "repo")
    _add_remote(repo, "origin", "https://github.com/owner/repo.git")

    identity = _resolve_identity(workspace, repo)

    assert identity == RepositoryIdentity(
        host="github.com",
        owner="owner",
        repository="repo",
        source="origin",
        remote_name="origin",
    )


def test_an_override_naming_a_specific_remote_resolves_from_that_remote(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _init_repo(workspace.root / "repo")
    _add_remote(repo, "origin", "https://github.com/decoy/decoy.git")
    _add_remote(repo, "upstream", "git@github.com:owner/repo.git")

    override = GithubTargetOverride(workspace_relative=Path("repo"), remote="upstream")
    identity = _resolve_identity(workspace, repo, github_targets=(override,))

    assert identity == RepositoryIdentity(
        host="github.com",
        owner="owner",
        repository="repo",
        source="remote",
        remote_name="upstream",
    )


def test_multiple_ambiguous_remotes_and_no_origin_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _init_repo(workspace.root / "repo")
    _add_remote(repo, "alpha", "https://github.com/owner-a/repo-a.git")
    _add_remote(repo, "beta", "https://github.com/owner-b/repo-b.git")

    with pytest.raises(PreflightError) as exc_info:
        _resolve_identity(workspace, repo)

    assert exc_info.value.code == "github.no_unique_identity"


def test_zero_remotes_and_no_override_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _init_repo(workspace.root / "repo")

    with pytest.raises(PreflightError) as exc_info:
        _resolve_identity(workspace, repo)

    assert exc_info.value.code == "github.no_unique_identity"


def test_a_populated_opencode_agents_directory_does_not_interfere_with_identity(
    tmp_path: Path,
) -> None:
    """A real target repository using this tool -- once M11-04 ships --
    carries a committed `.opencode/agents/` directory alongside its normal
    `origin` remote. Identity resolution reads Git remotes only (System
    Design SS17.1); it must succeed exactly as it does for any other
    tracked, committed content (M11-04, integration point named by the
    issue breakdown's own "File/componenti previsti" line for this
    milestone).
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _init_repo(workspace.root / "repo")
    _add_remote(repo, "origin", "https://github.com/owner/repo.git")

    agents_dir = repo / ".opencode" / "agents"
    agents_dir.mkdir(parents=True)
    for definition in sorted(AGENT_DEFINITIONS_DIR.glob("*.md")):
        (agents_dir / definition.name).write_text(
            definition.read_text(encoding="utf-8"), encoding="utf-8"
        )
    _git(["add", "-A"], cwd=repo)
    _git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "add opencode agent definitions",
        ],
        cwd=repo,
    )

    identity = _resolve_identity(workspace, repo)

    assert identity == RepositoryIdentity(
        host="github.com",
        owner="owner",
        repository="repo",
        source="origin",
        remote_name="origin",
    )


# =============================================================================
# gh preflight and locate_issue (M11-02; System Design SS17.2/SS17.3)
# =============================================================================


def _identity(*, host: str = "github.com") -> RepositoryIdentity:
    return RepositoryIdentity(
        host=host,
        owner="owner",
        repository="repo",
        source="origin",
        remote_name="origin",
    )


def _preflight(
    *, host: str = "github.com", gh_executable: Path = FAKE_GH
) -> GhPreflightEvidence:
    return run_gh_preflight(
        SubprocessRunner(RealClock()),
        gh_executable=gh_executable,
        host=host,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


def test_resolve_gh_executable_fails_closed_when_gh_is_not_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real, unmocked `PATH` containing no `gh` at all -- the "gh assente"
    row of System Design SS17.3's fail-closed table -- proven against the
    real environment rather than a mocked `shutil.which` (that pure variant
    lives in `tests/unit/test_github_identity.py`).
    """

    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(PreflightError) as exc_info:
        resolve_gh_executable()
    assert exc_info.value.code == "github.gh_executable_not_found"


def test_run_gh_preflight_succeeds_for_github_com(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FAKE_GH_VERSION_OUTPUT", raising=False)
    monkeypatch.delenv("FAKE_GH_AUTH_EXIT_CODE", raising=False)

    evidence = _preflight(host="github.com")

    assert evidence.host == "github.com"
    assert evidence.gh_version == "2.40.1"
    assert re.fullmatch(r"[0-9a-f]{64}", evidence.auth_status_digest)


def test_run_gh_preflight_succeeds_for_an_enterprise_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FAKE_GH_VERSION_OUTPUT", raising=False)
    monkeypatch.delenv("FAKE_GH_AUTH_EXIT_CODE", raising=False)

    evidence = _preflight(host="ghe.example.com")

    assert evidence.host == "ghe.example.com"
    assert evidence.gh_version == "2.40.1"


def test_run_gh_preflight_calls_version_then_auth_status_with_the_resolved_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    call_log = tmp_path / "gh-calls.log"
    monkeypatch.setenv("FAKE_GH_CALL_LOG_FILE", str(call_log))

    _preflight(host="ghe.example.com")

    lines = call_log.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "--version",
        "auth status --hostname ghe.example.com",
    ]


def test_run_gh_preflight_fails_closed_on_a_garbled_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    call_log = tmp_path / "gh-calls.log"
    monkeypatch.setenv("FAKE_GH_CALL_LOG_FILE", str(call_log))
    monkeypatch.setenv("FAKE_GH_VERSION_OUTPUT", "command not found: gh\n")

    with pytest.raises(PreflightError) as exc_info:
        _preflight()

    assert exc_info.value.code == "github.gh_version_unparseable"
    # A garbled version must short-circuit before `auth status` is ever run.
    assert call_log.read_text(encoding="utf-8").splitlines() == ["--version"]


def test_run_gh_preflight_fails_closed_when_gh_version_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_VERSION_EXIT_CODE", "1")

    with pytest.raises(PreflightError) as exc_info:
        _preflight()

    assert exc_info.value.code == "github.gh_version_probe_failed"


def test_run_gh_preflight_fails_closed_when_auth_status_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_marker = "SECRET_NOT_LOGGED_IN_TO_ANY_GITHUB_HOSTS_TOKEN_abc123"
    monkeypatch.setenv("FAKE_GH_AUTH_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_GH_AUTH_STDERR", f"X {secret_marker}\n")

    with pytest.raises(PreflightError) as exc_info:
        _preflight()

    error = exc_info.value
    assert error.code == "github.gh_auth_failed"
    assert secret_marker not in error.message
    assert error.technical_detail is not None
    assert secret_marker not in error.technical_detail
    assert secret_marker not in repr(error)
    assert secret_marker not in str(error)


def test_run_gh_preflight_fails_closed_when_auth_status_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_AUTH_SLEEP_SECONDS", "5")

    with pytest.raises(PreflightError) as exc_info:
        run_gh_preflight(
            SubprocessRunner(RealClock()),
            gh_executable=FAKE_GH,
            host="github.com",
            utility_timeout_seconds=SHORT_TIMEOUT_SECONDS,
            termination_grace_seconds=SHORT_GRACE_SECONDS,
        )

    error = exc_info.value
    assert error.code == "github.gh_auth_failed"
    assert error.technical_detail is not None
    assert "TIMEOUT" in error.technical_detail


def test_locate_issue_returns_a_locator_after_a_successful_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FAKE_GH_AUTH_EXIT_CODE", raising=False)
    identity = _identity(host="github.com")

    locator = locate_issue(
        identity,
        42,
        process_runner=SubprocessRunner(RealClock()),
        gh_executable=FAKE_GH,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )

    assert locator == IssueLocator(repository_identity=identity, number=42)


def test_locate_issue_never_constructs_a_locator_when_the_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_AUTH_EXIT_CODE", "1")
    identity = _identity(host="github.com")

    with pytest.raises(PreflightError) as exc_info:
        locate_issue(
            identity,
            42,
            process_runner=SubprocessRunner(RealClock()),
            gh_executable=FAKE_GH,
            utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
            termination_grace_seconds=TERMINATION_GRACE_SECONDS,
        )

    assert exc_info.value.code == "github.gh_auth_failed"


def test_locate_issue_rejects_a_non_positive_issue_number_as_a_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad `issue_number` is the caller's own contract violation, not a
    preflight fact: it must surface as `IssueLocator`'s own `ValueError`,
    never as a re-wrapped `PreflightError` -- and only after the preflight
    itself has already succeeded (SS17.2's own gh evidence is unaffected).
    """

    monkeypatch.delenv("FAKE_GH_AUTH_EXIT_CODE", raising=False)
    identity = _identity(host="github.com")

    with pytest.raises(ValueError) as exc_info:
        locate_issue(
            identity,
            0,
            process_runner=SubprocessRunner(RealClock()),
            gh_executable=FAKE_GH,
            utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
            termination_grace_seconds=TERMINATION_GRACE_SECONDS,
        )

    assert not isinstance(exc_info.value, PreflightError)


# =============================================================================
# AC-029: issue identity, architect handoff, and fail-closed paths
# =============================================================================


def _issue_ref_envelope(
    locator: IssueLocator,
    overrides: dict[str, object] | None = None,
    *,
    drop: str | None = None,
) -> str:
    """Build a JSON `ISSUE_REF_JSON` envelope matching `locator`, with
    optional field overrides/drops -- mirrors `tests/unit/test_protocol.py`'s
    own `_envelope` helper rather than hand-copying `protocol.py`'s schema.
    """

    identity = locator.repository_identity
    fields: dict[str, object] = {
        "schema_version": 1,
        "host": identity.host,
        "owner": identity.owner,
        "repository": identity.repository,
        "number": locator.number,
        "url": (
            f"https://{identity.host}/{identity.owner}/{identity.repository}"
            f"/issues/{locator.number}"
        ),
        "title": "A real issue title",
    }
    if overrides:
        fields.update(overrides)
    if drop is not None:
        del fields[drop]
    return json.dumps(fields)


def _ready_text(body: str, envelope: str) -> str:
    """An architect `READY` response carrying `envelope`, mirroring
    `tests/unit/test_protocol.py`'s own `_ready_text` helper."""

    return f"{body}\nISSUE_REF_JSON: {envelope}\nAGENT_STATUS: READY"


def test_ac_029_identity_architect_handoff_and_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-029 (PRD "Issue identity e handoff"): "in un fixture
    multi-repository, il repository GitHub viene derivato dal target o da
    override esplicito, l'architect viene istruito a usare `gh issue view`,
    un'identita ambigua/auth failure/not-found fallisce chiuso e un handoff
    riuscito contiene ISSUE_REF_JSON valido. Envelope e testo dell'handoff
    vengono validati e passati al coder senza interpretare il body della
    issue in Python; mismatch, JSON malformato o campo obbligatorio assente
    produce PROTOCOL_ERROR."

    This is the single canonical entry point the implementation plan names
    for AC-029 (SS6). It does not re-derive any of the underlying proofs --
    every clause below is exhaustively covered elsewhere already, and this
    test only assembles the already-proven real-process/pure entry points
    behind one recognizable node, one inline-commented clause per sentence
    of the acceptance criterion. AC-029 is itself explicitly a
    multi-milestone AC ("parte AC-029" in the implementation plan's own
    table, validated across M06/M07/M11/M13); the one clause this component
    file cannot legitimately own on its own -- the full pipeline-halts-
    the-coder behavior on a `not-found`-shaped architect failure, which
    needs the orchestrator/`SequencedAgentRunner` apparatus -- is instead
    cross-referenced below rather than duplicated.
    """

    workspace = _workspace(tmp_path / "workspace")

    # (1) "il repository GitHub viene derivato dal target" -- one repository
    # in a shared multi-repository fixture, identified from its `origin`
    # remote alone. Exhaustively proven already by
    # test_a_repository_with_a_valid_origin_resolves_from_origin; re-invoked
    # here against a sibling repository in the same workspace.
    origin_repo = _init_repo(workspace.root / "repo-origin")
    _add_remote(origin_repo, "origin", "https://github.com/owner/origin-repo.git")
    origin_identity = _resolve_identity(workspace, origin_repo)
    assert origin_identity == RepositoryIdentity(
        host="github.com",
        owner="owner",
        repository="origin-repo",
        source="origin",
        remote_name="origin",
    )

    # (1) "...o da override esplicito" -- a sibling repository in the same
    # multi-repository fixture, identified by an explicit remote override
    # instead. Exhaustively proven already by
    # test_an_override_naming_a_specific_remote_resolves_from_that_remote.
    override_repo = _init_repo(workspace.root / "repo-override")
    _add_remote(override_repo, "origin", "https://github.com/decoy/decoy.git")
    _add_remote(override_repo, "upstream", "git@github.com:owner/override-repo.git")
    override = GithubTargetOverride(
        workspace_relative=Path("repo-override"), remote="upstream"
    )
    override_identity = _resolve_identity(
        workspace, override_repo, github_targets=(override,)
    )
    assert override_identity == RepositoryIdentity(
        host="github.com",
        owner="owner",
        repository="override-repo",
        source="remote",
        remote_name="upstream",
    )

    # (2) "un'identita ambigua ... fallisce chiuso" -- a third repository in
    # the same fixture, with two remotes and no origin. Exhaustively proven
    # already by test_multiple_ambiguous_remotes_and_no_origin_fails_closed.
    ambiguous_repo = _init_repo(workspace.root / "repo-ambiguous")
    _add_remote(ambiguous_repo, "alpha", "https://github.com/owner-a/repo-a.git")
    _add_remote(ambiguous_repo, "beta", "https://github.com/owner-b/repo-b.git")
    with pytest.raises(PreflightError) as ambiguous_error:
        _resolve_identity(workspace, ambiguous_repo)
    assert ambiguous_error.value.code == "github.no_unique_identity"

    # (2) "...auth failure ... fallisce chiuso" -- a clean non-zero
    # `gh auth status` exit fails closed and never constructs a locator.
    # Exhaustively proven already by
    # test_run_gh_preflight_fails_closed_when_auth_status_exits_nonzero and
    # test_locate_issue_never_constructs_a_locator_when_the_preflight_fails.
    monkeypatch.delenv("FAKE_GH_VERSION_OUTPUT", raising=False)
    monkeypatch.setenv("FAKE_GH_AUTH_EXIT_CODE", "1")
    with pytest.raises(PreflightError) as auth_error:
        locate_issue(
            origin_identity,
            42,
            process_runner=SubprocessRunner(RealClock()),
            gh_executable=FAKE_GH,
            utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
            termination_grace_seconds=TERMINATION_GRACE_SECONDS,
        )
    assert auth_error.value.code == "github.gh_auth_failed"
    monkeypatch.delenv("FAKE_GH_AUTH_EXIT_CODE", raising=False)

    # A clean preflight, needed as the shared locator for every remaining
    # clause below.
    locator = locate_issue(
        origin_identity,
        42,
        process_runner=SubprocessRunner(RealClock()),
        gh_executable=FAKE_GH,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )

    # (3) "l'architect viene istruito a usare `gh issue view`" -- the
    # trusted architect prompt embeds the exact command shape built from
    # this target-specific locator. Exhaustively proven already by
    # tests/unit/test_prompting.py's golden prompt and its dedicated
    # command-shape assertions for both a github.com and an Enterprise
    # host; this file never otherwise touches `prompting.py`.
    architect_prompt = build_architect_prompt(
        issue_locator=locator,
        workspace_root=workspace.root,
        target_root=origin_repo,
    )
    identity_display = (
        f"{locator.repository_identity.owner}/{locator.repository_identity.repository}"
    )
    assert f"gh issue view {locator.number} --repo {identity_display}" in (
        architect_prompt
    )

    # (4) "un handoff riuscito contiene ISSUE_REF_JSON valido. Envelope e
    # testo dell'handoff vengono validati e passati al coder senza
    # interpretare il body della issue in Python" -- a valid envelope
    # produces an IssueRef matching the locator, and the untrusted,
    # multi-line handoff body is forwarded byte-for-byte -- Python never
    # parses or rewrites it -- into the coder prompt.
    handoff_body = (
        "Investigated the issue and confirmed the failing scenario.\n"
        "Plan: fix the retry policy and add regression tests."
    )
    valid_envelope = _issue_ref_envelope(locator)
    parsed = parse_agent_response(
        AgentRole.ARCHITECT,
        _ready_text(handoff_body, valid_envelope),
        issue_locator=locator,
    )
    assert parsed.agent_status is AgentStatus.READY
    assert parsed.body == handoff_body
    assert parsed.issue_ref is not None
    assert parsed.issue_ref == IssueRef(
        locator=locator,
        url=(
            f"https://{locator.repository_identity.host}"
            f"/{locator.repository_identity.owner}"
            f"/{locator.repository_identity.repository}/issues/{locator.number}"
        ),
        title="A real issue title",
    )

    coder_prompt = build_coder_prompt(
        issue_ref=parsed.issue_ref,
        architect_handoff=parsed.body,
        target_root=origin_repo,
        review_cycle=1,
        max_review_cycles=3,
    )
    assert handoff_body in coder_prompt

    # (5) "mismatch, JSON malformato o campo obbligatorio assente produce
    # PROTOCOL_ERROR" -- the full precedence matrix for this is already
    # exhaustively unit-tested in tests/unit/test_protocol.py
    # (test_malformed_json_syntax_is_rejected, test_a_missing_key_is_rejected,
    # test_identity_mismatch_against_the_locator_is_rejected); re-driven
    # here through the same real parser against this target-specific
    # locator to prove the link end to end within this file.
    with pytest.raises(ProtocolError) as malformed_json_error:
        parse_agent_response(
            AgentRole.ARCHITECT,
            _ready_text(handoff_body, "{not valid json"),
            issue_locator=locator,
        )
    assert malformed_json_error.value.code == "protocol.issue_ref_invalid_json"

    with pytest.raises(ProtocolError) as missing_key_error:
        parse_agent_response(
            AgentRole.ARCHITECT,
            _ready_text(handoff_body, _issue_ref_envelope(locator, drop="title")),
            issue_locator=locator,
        )
    assert missing_key_error.value.code == "protocol.issue_ref_missing_key"

    with pytest.raises(ProtocolError) as mismatch_error:
        parse_agent_response(
            AgentRole.ARCHITECT,
            _ready_text(
                handoff_body,
                _issue_ref_envelope(locator, {"host": "example.com"}),
            ),
            issue_locator=locator,
        )
    assert mismatch_error.value.code == "protocol.issue_ref_identity_mismatch"

    # (6) "not-found fallisce chiuso" -- Python's own locate_issue/
    # run_gh_preflight never call `gh issue view` at all (this file's own
    # module docstring, and protocol.py's/github.py's): only the architect
    # agent does, inside its own sandbox. The piece this component file can
    # legitimately own is that the *protocol* accepts and terminally
    # classifies a not-found-shaped architect failure as a normal,
    # non-exceptional agent-reported failure parse -- never an exception --
    # so the pipeline can proceed to its own AGENT_REPORTED_FAILURE
    # handling. The full pipeline-halts-the-coder proof for this same
    # scenario lives in
    # tests/component/test_single_issue_pipeline.py::
    # test_coder_is_not_invoked_when_the_architect_reports_failed.
    not_found_result = parse_agent_response(
        AgentRole.ARCHITECT,
        "Could not access the issue.\nAGENT_STATUS: FAILED",
        issue_locator=locator,
    )
    assert not_found_result.agent_status is AgentStatus.FAILED
    assert not_found_result.issue_ref is None

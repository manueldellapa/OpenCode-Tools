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

import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import (
    GithubTargetOverride,
    IssueLocator,
    RepositoryIdentity,
    Workspace,
)
from opencode_tools.errors import PreflightError
from opencode_tools.git_safety import resolve_git_executable, resolve_target
from opencode_tools.github import (
    GhPreflightEvidence,
    locate_issue,
    resolve_gh_executable,
    resolve_repository_identity,
    run_gh_preflight,
)
from opencode_tools.process import SubprocessRunner

UTILITY_TIMEOUT_SECONDS = 5.0
TERMINATION_GRACE_SECONDS = 1.0

GIT_EXECUTABLE = resolve_git_executable()
FAKE_GH = Path(__file__).resolve().parent / "helpers" / "fake_gh.py"

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

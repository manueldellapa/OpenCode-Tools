"""Component tests for `resolve_repository_identity`'s real Git wiring
(M11-01; ADR-007; System Design SS17.1).

Builds real temporary Git repositories, adds real remotes via `git remote
add`, and drives `resolve_repository_identity` through the real
`SubprocessRunner` and a real `git` executable -- mirrors
`tests/component/test_git_repository.py`'s setup style. This file proves
real-process wiring for a handful of representative scenarios only; the
exhaustive precedence/ambiguity matrix is unit-tested directly against
in-memory `RemoteFetchUrl` data in `tests/unit/test_github_identity.py`.
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import GithubTargetOverride, RepositoryIdentity, Workspace
from opencode_tools.errors import PreflightError
from opencode_tools.git_safety import resolve_git_executable, resolve_target
from opencode_tools.github import resolve_repository_identity
from opencode_tools.process import SubprocessRunner

UTILITY_TIMEOUT_SECONDS = 5.0
TERMINATION_GRACE_SECONDS = 1.0

GIT_EXECUTABLE = resolve_git_executable()


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

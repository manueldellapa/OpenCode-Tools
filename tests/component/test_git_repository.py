"""Component tests for the read-only Git target preflight (M09-01).

Builds real temporary Git repositories and drives `resolve_target` through
the real `SubprocessRunner` and a real `git` executable -- never a fixture
or mock of `ProcessRunner` itself -- to prove AC-004 (path safety), AC-005
(dirty preflight), AC-006 (Git shape), and AC-036 (a Git probe failure fails
closed). `tests/component/helpers/fake_git.py` stands in for `git` only in
the AC-036 cases, where a real hung or corrupted repository cannot be
constructed deterministically.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import TargetRepository, Workspace
from opencode_tools.errors import PreflightError
from opencode_tools.git_safety import resolve_git_executable, resolve_target
from opencode_tools.process import SubprocessRunner

FAKE_GIT = Path(__file__).resolve().parent / "helpers" / "fake_git.py"

UTILITY_TIMEOUT_SECONDS = 5.0
TERMINATION_GRACE_SECONDS = 1.0

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


class RealClock:
    """A `Clock` reading genuine wall/monotonic time for a real subprocess."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


GIT_EXECUTABLE = resolve_git_executable()


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)


def _commit_all(root: Path, message: str) -> None:
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", message], cwd=root)


def _clean_repo(root: Path) -> Path:
    _init_repo(root)
    (root / "file.txt").write_text("hello\n", encoding="utf-8")
    _commit_all(root, "initial")
    return root


def _workspace(root: Path) -> Workspace:
    root.mkdir(parents=True, exist_ok=True)
    return Workspace(root=root.resolve())


def _resolve(
    workspace: Workspace, target_root: Path, *, git_executable: Path = GIT_EXECUTABLE
) -> TargetRepository:
    return resolve_target(
        SubprocessRunner(RealClock()),
        git_executable=git_executable,
        workspace=workspace,
        target_root=target_root,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


# --- happy path ------------------------------------------------------------


def test_resolve_target_accepts_a_clean_top_level_repository(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")

    target = _resolve(workspace, repo)

    assert target.root == repo.resolve()
    assert target.workspace_relative == Path("repo")
    assert target.git_common_dir == (repo / ".git").resolve()


def test_resolve_target_accepts_the_workspace_itself_as_the_target(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    _clean_repo(workspace.root)

    target = _resolve(workspace, workspace.root)

    assert target.root == workspace.root
    assert target.workspace_relative == Path(".")


def test_resolve_target_accepts_a_repository_with_only_an_ignored_file(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = workspace.root / "repo"
    _clean_repo(repo)
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _commit_all(repo, "add gitignore")
    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    target = _resolve(workspace, repo)

    assert target.root == repo.resolve()


# --- AC-004: path safety ----------------------------------------------------


def test_ac_004_path_containment_symlink_and_top_level(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")

    # A target outside the workspace is rejected before any Git probe runs.
    outside = _clean_repo(tmp_path / "outside")
    with pytest.raises(PreflightError) as outside_error:
        _resolve(workspace, outside)
    assert outside_error.value.code == "git_safety.target_escapes_workspace"

    # A symlink inside the workspace that resolves outside it is rejected
    # the same way, after symlink resolution.
    symlink = workspace.root / "escape_link"
    symlink.symlink_to(outside)
    with pytest.raises(PreflightError) as symlink_error:
        _resolve(workspace, symlink)
    assert symlink_error.value.code == "git_safety.target_escapes_workspace"

    # A target contained in the workspace but not itself the Git top-level
    # (a subdirectory of a repository) is rejected.
    repo = _clean_repo(workspace.root / "repo")
    subdir = repo / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("nested\n", encoding="utf-8")
    _commit_all(repo, "add subdir")
    with pytest.raises(PreflightError) as nested_error:
        _resolve(workspace, subdir)
    assert nested_error.value.code == "git_safety.not_git_top_level"


def test_resolve_target_rejects_a_target_that_does_not_exist(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")

    with pytest.raises(PreflightError) as exc_info:
        _resolve(workspace, workspace.root / "missing")
    assert exc_info.value.code == "git_safety.target_not_found"


# --- AC-005: dirty preflight matrix -----------------------------------------


def _stage_a_change(repo: Path) -> None:
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")
    _git(["add", "file.txt"], cwd=repo)


def _leave_a_change_unstaged(repo: Path) -> None:
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")


def _add_an_untracked_file(repo: Path) -> None:
    (repo / "new.txt").write_text("new\n", encoding="utf-8")


@pytest.mark.parametrize(
    "make_dirty",
    [
        pytest.param(_stage_a_change, id="staged"),
        pytest.param(_leave_a_change_unstaged, id="unstaged"),
        pytest.param(_add_an_untracked_file, id="untracked"),
    ],
)
def test_ac_005_dirty_preflight_matrix(
    tmp_path: Path, make_dirty: Callable[[Path], None]
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")

    make_dirty(repo)

    with pytest.raises(PreflightError) as exc_info:
        _resolve(workspace, repo)
    assert exc_info.value.code == "git_safety.dirty_worktree"


# --- AC-006: Git shape -------------------------------------------------------


def test_ac_006_bare_detached_and_unborn_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")

    # Bare repository: no working tree at all, so `--show-toplevel` (the
    # first probe, per System Design SS11.1's order) fails outright before
    # `--is-bare-repository` ever runs.
    bare = workspace.root / "bare"
    bare.mkdir()
    _git(["init", "--quiet", "--bare"], cwd=bare)
    with pytest.raises(PreflightError) as bare_error:
        _resolve(workspace, bare)
    assert bare_error.value.code == "git_safety.top_level_probe_failed"

    # Detached HEAD: checked out directly onto a commit, not a branch.
    detached = _clean_repo(workspace.root / "detached")
    sha = _git(["rev-parse", "HEAD"], cwd=detached).stdout.decode("utf-8").strip()
    _git(["checkout", "--quiet", sha], cwd=detached)
    with pytest.raises(PreflightError) as detached_error:
        _resolve(workspace, detached)
    assert detached_error.value.code == "git_safety.detached_head"

    # Unborn branch: initialized, but no commit yet, so HEAD does not
    # resolve to a commit.
    unborn = workspace.root / "unborn"
    _init_repo(unborn)
    with pytest.raises(PreflightError) as unborn_error:
        _resolve(workspace, unborn)
    assert unborn_error.value.code == "git_safety.head_not_resolvable"


# --- AC-036: an ambiguous/timed-out/non-zero probe fails closed -------------


def test_ac_036_git_probe_failure_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    target = workspace.root / "repo"
    target.mkdir()

    # A probe that exceeds the utility deadline fails closed.
    monkeypatch.setenv("FAKE_GIT_SLEEP_SECONDS", "5")
    with pytest.raises(PreflightError) as timeout_error:
        resolve_target(
            SubprocessRunner(RealClock()),
            git_executable=FAKE_GIT,
            workspace=workspace,
            target_root=target,
            utility_timeout_seconds=0.2,
            termination_grace_seconds=TERMINATION_GRACE_SECONDS,
        )
    assert timeout_error.value.code == "git_safety.top_level_probe_failed"
    monkeypatch.delenv("FAKE_GIT_SLEEP_SECONDS", raising=False)

    # A non-zero exit from a probe fails closed.
    monkeypatch.setenv("FAKE_GIT_EXIT_CODE", "128")
    with pytest.raises(PreflightError) as nonzero_error:
        _resolve(workspace, target, git_executable=FAKE_GIT)
    assert nonzero_error.value.code == "git_safety.top_level_probe_failed"
    monkeypatch.delenv("FAKE_GIT_EXIT_CODE", raising=False)

    # Ambiguous (non-UTF-8) probe output fails closed even on a zero exit.
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"\xff\xfe\x00not-utf8")
    monkeypatch.setenv("FAKE_GIT_STDOUT_FILE", str(garbage))
    with pytest.raises(PreflightError) as ambiguous_error:
        _resolve(workspace, target, git_executable=FAKE_GIT)
    assert ambiguous_error.value.code == "git_safety.top_level_probe_failed"

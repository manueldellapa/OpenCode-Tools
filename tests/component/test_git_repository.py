"""Component tests for the read-only Git target preflight (M09-01), the
`git-state-v1` fingerprint's real-repository wiring, and bounded,
fail-closed sampling stability (M09-03).

Builds real temporary Git repositories and drives `resolve_target`/
`capture_git_state` through the real `SubprocessRunner` and a real `git`
executable -- never a fixture or mock of `ProcessRunner` itself -- to prove
AC-004 (path safety), AC-005 (dirty preflight), AC-006 (Git shape), AC-036
(a Git probe failure fails closed), and M09-03's race/error/deadline
handling. `tests/component/helpers/fake_git.py` stands in for `git` only in
the AC-036 cases, where a real hung or corrupted repository cannot be
constructed deterministically; M09-03's own race and deadline scenarios use
a real repository with `monkeypatch` forcing the specific internal signal
(an unstable `lstat` bracket) that a real concurrent writer would produce,
since a genuine timing race would make the test flaky rather than
deterministic.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools import git_safety
from opencode_tools.domain import GitSafetyStatus, TargetRepository, Workspace
from opencode_tools.errors import PreflightError
from opencode_tools.git_safety import (
    capture_git_state,
    resolve_git_executable,
    resolve_target,
)
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


def _capture(
    target: TargetRepository,
    *,
    utility_timeout_seconds: float = UTILITY_TIMEOUT_SECONDS,
) -> git_safety.GitStateCapture:
    return capture_git_state(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        target=target,
        clock=RealClock(),
        utility_timeout_seconds=utility_timeout_seconds,
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


# =============================================================================
# M09-03: capture_git_state -- bounded, fail-closed sampling stability
# =============================================================================


def test_capture_git_state_returns_safe_for_a_stable_clean_repository(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    capture = _capture(target)

    assert capture.safety_status is GitSafetyStatus.SAFE
    assert capture.state.branch == "main"
    assert capture.state.head
    assert capture.state.fingerprint is not None
    # A clean repository has an empty inventory (M09-05).
    assert capture.state.staged == ()
    assert capture.state.unstaged == ()
    assert capture.state.untracked == ()


def test_capture_git_state_is_deterministic_across_repeated_calls(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    first = _capture(target)
    second = _capture(target)

    assert first.state.fingerprint == second.state.fingerprint


def test_capture_git_state_retries_once_on_a_first_instability_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    real_capture = git_safety._capture_path_entry
    calls = {"n": 0}

    def flaky_capture(
        target_root: Path, raw_path: bytes
    ) -> tuple[git_safety.FingerprintPathEntry, bool]:
        calls["n"] += 1
        entry, stable = real_capture(target_root, raw_path)
        if calls["n"] == 1:
            return entry, False
        return entry, stable

    monkeypatch.setattr(git_safety, "_capture_path_entry", flaky_capture)

    capture = _capture(target)

    assert capture.safety_status is GitSafetyStatus.SAFE
    assert capture.state.fingerprint is not None
    assert calls["n"] == 2


def test_capture_git_state_returns_indeterminate_after_a_second_instability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    real_capture = git_safety._capture_path_entry

    def always_unstable(
        target_root: Path, raw_path: bytes
    ) -> tuple[git_safety.FingerprintPathEntry, bool]:
        entry, _stable = real_capture(target_root, raw_path)
        return entry, False

    monkeypatch.setattr(git_safety, "_capture_path_entry", always_unstable)

    capture = _capture(target)

    assert capture.safety_status is GitSafetyStatus.INDETERMINATE
    assert capture.state.fingerprint is None
    assert capture.state.branch is None
    assert capture.state.head is None


def test_capture_git_state_returns_indeterminate_on_a_permission_failure(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = workspace.root / "repo"
    _init_repo(repo)
    secret = repo / "secret.txt"
    secret.write_text("shh\n", encoding="utf-8")
    _commit_all(repo, "add secret")
    target = _resolve(workspace, repo)

    secret.chmod(0o000)
    try:
        capture = _capture(target)
        assert capture.safety_status is GitSafetyStatus.INDETERMINATE
    finally:
        secret.chmod(0o644)


def test_capture_git_state_returns_indeterminate_on_a_special_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git's own working-tree scan silently skips FIFOs, sockets, and
    device files -- neither `ls-files --others` nor `status` ever lists
    one (verified directly against real `git`), so this path can only be
    reached via a race that replaces an already-listed file with a special
    one. `_capture_path_entry` is forced to report `"other"` directly,
    exactly the outcome such a race would produce.
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    real_capture = git_safety._capture_path_entry

    def special_file_capture(
        target_root: Path, raw_path: bytes
    ) -> tuple[git_safety.FingerprintPathEntry, bool]:
        entry, stable = real_capture(target_root, raw_path)
        return replace(entry, type="other", content_hash=None), stable

    monkeypatch.setattr(git_safety, "_capture_path_entry", special_file_capture)

    capture = _capture(target)

    assert capture.safety_status is GitSafetyStatus.INDETERMINATE


def test_capture_git_state_returns_indeterminate_on_a_path_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    def escaping_capture(
        target_root: Path, raw_path: bytes
    ) -> tuple[git_safety.FingerprintPathEntry, bool]:
        raise PreflightError(
            "git_safety.fingerprint_path_escapes_target", "forced for test"
        )

    monkeypatch.setattr(git_safety, "_capture_path_entry", escaping_capture)

    capture = _capture(target)

    assert capture.safety_status is GitSafetyStatus.INDETERMINATE


class _FakeDeadlineClock:
    """A `Clock` whose second `monotonic_ns()` call reports far past any
    deadline, so `capture_git_state`'s retry-gate deterministically skips
    the retry -- real wall-clock timing would make this flaky."""

    def __init__(self) -> None:
        self._calls = 0

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        self._calls += 1
        return 0 if self._calls == 1 else 10**18


def test_capture_git_state_does_not_retry_past_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    real_capture = git_safety._capture_path_entry

    def always_unstable(
        target_root: Path, raw_path: bytes
    ) -> tuple[git_safety.FingerprintPathEntry, bool]:
        entry, _stable = real_capture(target_root, raw_path)
        return entry, False

    monkeypatch.setattr(git_safety, "_capture_path_entry", always_unstable)

    capture = capture_git_state(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        target=target,
        clock=_FakeDeadlineClock(),
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )

    assert capture.safety_status is GitSafetyStatus.INDETERMINATE
    # Only the first attempt's twelve probes (six initial + six resample)
    # ran; the deadline had already passed before a second attempt could be
    # considered.
    assert len(capture.process_results) == 12


# =============================================================================
# M09-05: change inventory (staged/unstaged/untracked preservation)
# =============================================================================


def test_capture_git_state_inventories_staged_unstaged_and_untracked(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    _commit_all(repo, "add second file")
    target = _resolve(workspace, repo)

    # Dirty the target the same way an in-progress coder attempt would,
    # after the target has already been resolved as a clean top-level repo.
    (repo / "file.txt").write_text("staged change\n", encoding="utf-8")
    _git(["add", "file.txt"], cwd=repo)
    (repo / "second.txt").write_text("unstaged change\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    capture = _capture(target)

    assert capture.safety_status is GitSafetyStatus.SAFE
    assert capture.state.staged == ("file.txt",)
    assert capture.state.unstaged == ("second.txt",)
    assert capture.state.untracked == ("new.txt",)
    # Nothing here mutates or cleans up the target -- every file is exactly
    # as this test itself left it.
    assert (repo / "file.txt").read_text(encoding="utf-8") == "staged change\n"
    assert (repo / "second.txt").read_text(encoding="utf-8") == "unstaged change\n"
    assert (repo / "new.txt").read_text(encoding="utf-8") == "new\n"


# =============================================================================
# M09-06 [GATE BLOCCANTE M09]: git-state-v1 large-repository scalability
# =============================================================================
#
# See docs/git-state-v1-scalability.md for the recorded qualification
# evidence (corpus description, OS/Python/Git versions,
# utility_timeout_seconds, repetition count, observed durations, and the
# PASS/BLOCKED outcome). This test is the qualification's repeatable
# measurement and this repository's ongoing regression guard for the two
# properties that evidence depends on: a deterministic digest across
# repeated captures of the same corpus, and completion within a generous,
# canonical-range timeout. The companion test below proves the
# already-existing fail-closed behavior (M09-03) still holds for this
# corpus shape when the deadline is exceeded -- there is no fallback to
# porcelain-only hashing.

_QUALIFICATION_DIRECTORY_COUNT = 50
_QUALIFICATION_FILES_PER_DIRECTORY = 100
_QUALIFICATION_FILE_COUNT = (
    _QUALIFICATION_DIRECTORY_COUNT * _QUALIFICATION_FILES_PER_DIRECTORY
)
_QUALIFICATION_REPETITIONS = 3
_QUALIFICATION_TIMEOUT_SECONDS = 30.0


def _build_large_repository(root: Path) -> Path:
    _init_repo(root)
    for directory_index in range(_QUALIFICATION_DIRECTORY_COUNT):
        directory = root / f"dir{directory_index:03d}"
        directory.mkdir()
        for file_index in range(_QUALIFICATION_FILES_PER_DIRECTORY):
            (directory / f"file{file_index:04d}.txt").write_text(
                f"content {directory_index}-{file_index}\n", encoding="utf-8"
            )
    _commit_all(root, f"large corpus: {_QUALIFICATION_FILE_COUNT} files")
    return root


def test_git_state_v1_large_repository_qualification(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _build_large_repository(workspace.root / "repo")
    target = _resolve(workspace, repo)

    digests = []
    durations = []
    for _repetition in range(_QUALIFICATION_REPETITIONS):
        started = time.monotonic()
        capture = capture_git_state(
            SubprocessRunner(RealClock()),
            git_executable=GIT_EXECUTABLE,
            target=target,
            clock=RealClock(),
            utility_timeout_seconds=_QUALIFICATION_TIMEOUT_SECONDS,
            termination_grace_seconds=TERMINATION_GRACE_SECONDS,
        )
        durations.append(time.monotonic() - started)
        assert capture.safety_status is GitSafetyStatus.SAFE
        digests.append(capture.state.fingerprint)

    # Repeatability: an unchanged corpus produces an identical digest on
    # every repetition (AC-036's "ripetibilità del digest").
    assert len(set(digests)) == 1

    # Completion within the configured deadline, on every repetition --
    # this test's own regression guard; docs/git-state-v1-scalability.md
    # records the actual observed durations as the qualification evidence,
    # since no additional SLO is fixed by the canonical sources.
    assert all(duration < _QUALIFICATION_TIMEOUT_SECONDS for duration in durations)


def test_git_state_v1_exceeded_deadline_on_a_large_repository_is_indeterminate(
    tmp_path: Path,
) -> None:
    """An exceeded deadline fails closed to `INDETERMINATE` -- never a
    less-safe fallback to porcelain-only hashing -- even for a large
    corpus (AC-036, System Design SS11.2).
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _build_large_repository(workspace.root / "repo")
    target = _resolve(workspace, repo)

    capture = capture_git_state(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        target=target,
        clock=RealClock(),
        utility_timeout_seconds=0.001,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )

    assert capture.safety_status is GitSafetyStatus.INDETERMINATE
    assert capture.state.fingerprint is None

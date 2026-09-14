"""Component tests for checkpoint before/after and role mutation policy
(M09-04).

Builds real temporary Git repositories and drives `check_git_state` through
the real `SubprocessRunner` and a real `git` executable -- never a fixture
or mock of `ProcessRunner` itself -- to prove AC-013 (a coder's fingerprint
delta is inventoried and suppresses retry, never hidden), AC-018
(branch/HEAD drift blocks new invocations), AC-028 (a read-only role's
mutation is unsafe), and AC-036 (an indeterminate capture never becomes a
safe checkpoint).

Every `before`/continuity check below passes `role=None` deliberately: a
provider attempt's own role tolerance is scoped to its own `after` versus
its own `before` (see `check_git_state`'s docstring) and must never be
applied when comparing a `before` against the *last accepted* checkpoint,
or an external mutation between phases would be incorrectly excused as the
upcoming role's own doing.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools import git_safety
from opencode_tools.domain import (
    AgentRole,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    TargetRepository,
    Workspace,
)
from opencode_tools.git_safety import (
    check_git_state,
    resolve_git_executable,
    resolve_target,
    target_fingerprint_changed,
)
from opencode_tools.process import SubprocessRunner

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


def _resolve(workspace: Workspace, target_root: Path) -> TargetRepository:
    return resolve_target(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        workspace=workspace,
        target_root=target_root,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


def _check(
    target: TargetRepository,
    *,
    sequence: int,
    purpose: str,
    role: AgentRole | None = None,
    baseline: GitState | None = None,
) -> GitCheckRecord:
    return check_git_state(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        target=target,
        clock=RealClock(),
        sequence=sequence,
        purpose=purpose,
        role=role,
        baseline=baseline,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


# --- the first-ever checkpoint (no baseline) --------------------------------


def test_check_git_state_first_checkpoint_is_safe_with_no_baseline(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    record = _check(target, sequence=0, purpose="baseline")

    assert record.safety_status is GitSafetyStatus.SAFE
    assert record.compared_to is None
    assert record.state.branch == "main"
    assert record.state.fingerprint is not None
    assert target_fingerprint_changed(record) is False


# --- continuity: drift after every role -------------------------------------


def test_check_git_state_detects_branch_drift_before_the_next_spawn(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    baseline = _check(target, sequence=0, purpose="baseline")

    _git(["checkout", "--quiet", "-b", "other"], cwd=repo)

    record = _check(
        target,
        sequence=1,
        purpose="coder-attempt-1-before",
        role=None,
        baseline=baseline.state,
    )

    assert record.safety_status is GitSafetyStatus.UNSAFE


def test_check_git_state_detects_head_drift_before_the_next_spawn(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    baseline = _check(target, sequence=0, purpose="baseline")

    (repo / "extra.txt").write_text("extra\n", encoding="utf-8")
    _commit_all(repo, "extra commit")

    record = _check(
        target,
        sequence=1,
        purpose="coder-attempt-1-before",
        role=None,
        baseline=baseline.state,
    )

    assert record.safety_status is GitSafetyStatus.UNSAFE


def test_check_git_state_detects_a_mutation_between_two_phases(tmp_path: Path) -> None:
    """A delta introduced between phases -- during backoff or a
    control-plane recheck, say -- is caught on the next phase's `before`
    check (role=None), prior to any new spawn, and is never excused just
    because the upcoming role happens to be the coder.
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    architect_after = _check(
        target,
        sequence=0,
        purpose="architect-attempt-1-after",
        role=AgentRole.ARCHITECT,
    )

    # An external mutation, not attributable to any agent role.
    (repo / "file.txt").write_text("external mutation\n", encoding="utf-8")

    coder_before = _check(
        target,
        sequence=1,
        purpose="coder-attempt-1-before",
        role=None,
        baseline=architect_after.state,
    )

    assert coder_before.safety_status is GitSafetyStatus.UNSAFE


# --- role mutation policy: read-only roles ----------------------------------


@pytest.mark.parametrize("role", [AgentRole.ARCHITECT, AgentRole.REVIEWER])
def test_check_git_state_read_only_role_delta_is_unsafe(
    tmp_path: Path, role: AgentRole
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    before = _check(target, sequence=0, purpose=f"{role.value}-attempt-1-before")

    # The permission policy denies edit to architect/reviewer; this
    # simulates the mutation this check must still catch regardless.
    (repo / "file.txt").write_text("mutated\n", encoding="utf-8")

    after = _check(
        target,
        sequence=1,
        purpose=f"{role.value}-attempt-1-after",
        role=role,
        baseline=before.state,
    )

    assert after.safety_status is GitSafetyStatus.UNSAFE


# --- role mutation policy: coder --------------------------------------------


def test_check_git_state_coder_success_delta_is_safe_and_inventoried(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    before = _check(target, sequence=0, purpose="coder-attempt-1-before")

    (repo / "file.txt").write_text("coder edit\n", encoding="utf-8")

    after = _check(
        target,
        sequence=1,
        purpose="coder-attempt-1-after",
        role=AgentRole.CODER,
        baseline=before.state,
    )

    assert after.safety_status is GitSafetyStatus.SAFE
    assert target_fingerprint_changed(after) is True


def test_check_git_state_coder_partial_mutation_is_preserved_and_detected(
    tmp_path: Path,
) -> None:
    """A coder attempt that only partially edits the target (e.g. before a
    provider error cuts it short) still reports the delta -- Python never
    cleans it up, and `target_fingerprint_changed` lets the retry layer
    suppress the retry.
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    before = _check(target, sequence=0, purpose="coder-attempt-1-before")

    (repo / "partial.txt").write_text("only half done\n", encoding="utf-8")

    after = _check(
        target,
        sequence=1,
        purpose="coder-attempt-1-after",
        role=AgentRole.CODER,
        baseline=before.state,
    )

    assert after.safety_status is GitSafetyStatus.SAFE
    assert target_fingerprint_changed(after) is True
    assert (repo / "partial.txt").read_text(encoding="utf-8") == "only half done\n"


def test_check_git_state_coder_no_delta_reports_no_target_change(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    before = _check(target, sequence=0, purpose="coder-attempt-1-before")

    after = _check(
        target,
        sequence=1,
        purpose="coder-attempt-1-after",
        role=AgentRole.CODER,
        baseline=before.state,
    )

    assert after.safety_status is GitSafetyStatus.SAFE
    assert target_fingerprint_changed(after) is False


# --- technical failures still get a checkpoint ------------------------------


def test_check_git_state_records_before_and_after_around_a_failed_attempt(
    tmp_path: Path,
) -> None:
    """Checkpoints are captured immediately before and after every provider
    attempt regardless of the attempt's own outcome -- simulated here by
    simply not invoking any agent between the two `check_git_state` calls,
    as if the attempt had failed to spawn or timed out in between.
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    before = _check(target, sequence=0, purpose="architect-attempt-1-before")
    after = _check(
        target,
        sequence=1,
        purpose="architect-attempt-1-after",
        role=AgentRole.ARCHITECT,
        baseline=before.state,
    )

    assert before.safety_status is GitSafetyStatus.SAFE
    assert after.safety_status is GitSafetyStatus.SAFE


def test_check_git_state_propagates_an_indeterminate_capture(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    before = _check(target, sequence=0, purpose="coder-attempt-1-before")

    secret = repo / "secret.txt"
    secret.write_text("shh\n", encoding="utf-8")
    _commit_all(repo, "add secret")
    secret.chmod(0o000)
    try:
        after = _check(
            target,
            sequence=1,
            purpose="coder-attempt-1-after",
            role=AgentRole.CODER,
            baseline=before.state,
        )
        assert after.safety_status is GitSafetyStatus.INDETERMINATE
        assert target_fingerprint_changed(after) is False
    finally:
        secret.chmod(0o644)


def test_check_git_state_first_checkpoint_is_indeterminate_when_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first-ever checkpoint (`baseline=None`) must never be guessed
    `SAFE` from an incomplete capture (e.g. a stably detached `HEAD`) --
    unreachable through the real M09-01 gate, which already rejects a
    detached target, so this forces the scenario directly.
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    real_capture = git_safety.capture_git_state

    def detached_but_stable(*args, **kwargs):  # type: ignore[no-untyped-def]
        capture = real_capture(*args, **kwargs)
        from dataclasses import replace

        return replace(capture, state=replace(capture.state, branch=None))

    monkeypatch.setattr(git_safety, "capture_git_state", detached_but_stable)

    record = _check(target, sequence=0, purpose="baseline")

    assert record.safety_status is GitSafetyStatus.INDETERMINATE


# =============================================================================
# M09-05: postflight -- check_git_state against the run's original baseline
# =============================================================================
#
# Postflight is check_git_state itself: called with the run's original
# baseline and role=AgentRole.CODER, so an expected coder delta over the
# whole run stays SAFE while branch/HEAD drift -- System Design SS8.4's
# final gate condition -- still fails it.


def test_check_git_state_postflight_tolerates_the_runs_own_coder_delta(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    run_baseline = _check(target, sequence=0, purpose="baseline")

    (repo / "file.txt").write_text("coder edit\n", encoding="utf-8")

    postflight = _check(
        target,
        sequence=1,
        purpose="postflight",
        role=AgentRole.CODER,
        baseline=run_baseline.state,
    )

    assert postflight.safety_status is GitSafetyStatus.SAFE
    assert postflight.state.branch == run_baseline.state.branch
    assert postflight.state.head == run_baseline.state.head


def test_check_git_state_postflight_detects_drift_after_reviewer_approval(
    tmp_path: Path,
) -> None:
    """Drift discovered only at postflight must still block the final
    APPROVED gate even though an earlier checkpoint (standing in for the
    reviewer's own APPROVED decision) was itself SAFE -- drift prevails
    over an already-recorded approval (System Design SS8.4).
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    run_baseline = _check(target, sequence=0, purpose="baseline")

    # The coder's own delta is accepted as the new last-accepted checkpoint.
    (repo / "file.txt").write_text("coder edit\n", encoding="utf-8")
    coder_after = _check(
        target,
        sequence=1,
        purpose="coder-attempt-1-after",
        role=AgentRole.CODER,
        baseline=run_baseline.state,
    )
    assert coder_after.safety_status is GitSafetyStatus.SAFE

    # The reviewer makes no edits of its own -- a clean APPROVED decision.
    reviewer_checkpoint = _check(
        target,
        sequence=2,
        purpose="reviewer-attempt-1-after-approved",
        role=AgentRole.REVIEWER,
        baseline=coder_after.state,
    )
    assert reviewer_checkpoint.safety_status is GitSafetyStatus.SAFE

    # Something moves HEAD after the (simulated) approval -- e.g. an
    # external process -- before postflight runs.
    _git(["commit", "--quiet", "--allow-empty", "-m", "drift after approval"], cwd=repo)

    postflight = _check(
        target,
        sequence=3,
        purpose="postflight",
        role=AgentRole.CODER,
        baseline=run_baseline.state,
    )

    assert postflight.safety_status is GitSafetyStatus.UNSAFE


def test_check_git_state_postflight_probe_failure_is_indeterminate(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    run_baseline = _check(target, sequence=0, purpose="baseline")

    secret = repo / "secret.txt"
    secret.write_text("shh\n", encoding="utf-8")
    _commit_all(repo, "add secret")
    secret.chmod(0o000)
    try:
        postflight = _check(
            target,
            sequence=1,
            purpose="postflight",
            role=AgentRole.CODER,
            baseline=run_baseline.state,
        )
        assert postflight.safety_status is GitSafetyStatus.INDETERMINATE
    finally:
        secret.chmod(0o644)


def test_check_git_state_postflight_preserves_a_partial_coder_output(
    tmp_path: Path,
) -> None:
    """A run that ends mid-attempt (e.g. a provider error cut the coder
    short) still gets a postflight that preserves and reports the partial
    edit -- Python never cleans it up, and it is never mistaken for drift.
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)
    run_baseline = _check(target, sequence=0, purpose="baseline")

    (repo / "partial.txt").write_text("only half done\n", encoding="utf-8")

    postflight = _check(
        target,
        sequence=1,
        purpose="postflight",
        role=AgentRole.CODER,
        baseline=run_baseline.state,
    )

    assert postflight.safety_status is GitSafetyStatus.SAFE
    assert postflight.state.untracked == ("partial.txt",)
    assert target_fingerprint_changed(postflight) is True
    assert (repo / "partial.txt").read_text(encoding="utf-8") == "only half done\n"

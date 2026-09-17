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
from collections.abc import Callable
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

# A controllable fake `git`, used only for AC-036's checkpoint-layer probe
# failures below -- see `tests/component/helpers/fake_git.py`.
FAKE_GIT = Path(__file__).resolve().parent / "helpers" / "fake_git.py"


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
    git_executable: Path = GIT_EXECUTABLE,
    utility_timeout_seconds: float = UTILITY_TIMEOUT_SECONDS,
) -> GitCheckRecord:
    return check_git_state(
        SubprocessRunner(RealClock()),
        git_executable=git_executable,
        target=target,
        clock=RealClock(),
        sequence=sequence,
        purpose=purpose,
        role=role,
        baseline=baseline,
        utility_timeout_seconds=utility_timeout_seconds,
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


# --- AC-018: branch/HEAD drift blocks new invocations, after each role -----


@pytest.mark.parametrize(
    "role", [AgentRole.ARCHITECT, AgentRole.CODER, AgentRole.REVIEWER]
)
def test_ac_018_branch_or_head_drift_after_each_role(
    tmp_path: Path, role: AgentRole
) -> None:
    """`check_git_state`'s own docstring claims branch/HEAD drift is
    `UNSAFE` "regardless of `role`" -- the continuity tests above already
    exercise that code path, but always with `role=None` (a `before`
    check). This proves the claim explicitly on a specific role's own
    `after` checkpoint, for both drift kinds, mirroring
    `test_check_git_state_detects_branch_drift_before_the_next_spawn` and
    `test_check_git_state_detects_head_drift_before_the_next_spawn`.

    Only the `GitSafetyStatus.UNSAFE` half of AC-018 belongs here: the
    "terminano FAILED/GIT_SAFETY_ERROR senza reset" clause is a
    state-machine/orchestrator-level final-outcome decision (this module
    only ever returns a `GitSafetyStatus`), out of `check_git_state`'s own
    contract.
    """

    # Branch drift.
    branch_workspace = _workspace(tmp_path / "branch-workspace")
    branch_repo = _clean_repo(branch_workspace.root / "repo")
    branch_target = _resolve(branch_workspace, branch_repo)
    branch_before = _check(
        branch_target, sequence=0, purpose=f"{role.value}-attempt-1-before"
    )

    _git(["checkout", "--quiet", "-b", "other"], cwd=branch_repo)

    branch_after = _check(
        branch_target,
        sequence=1,
        purpose=f"{role.value}-attempt-1-after",
        role=role,
        baseline=branch_before.state,
    )
    assert branch_after.safety_status is GitSafetyStatus.UNSAFE

    # HEAD drift.
    head_workspace = _workspace(tmp_path / "head-workspace")
    head_repo = _clean_repo(head_workspace.root / "repo")
    head_target = _resolve(head_workspace, head_repo)
    head_before = _check(
        head_target, sequence=0, purpose=f"{role.value}-attempt-1-before"
    )

    (head_repo / "extra.txt").write_text("extra\n", encoding="utf-8")
    _commit_all(head_repo, "extra commit")

    head_after = _check(
        head_target,
        sequence=1,
        purpose=f"{role.value}-attempt-1-after",
        role=role,
        baseline=head_before.state,
    )
    assert head_after.safety_status is GitSafetyStatus.UNSAFE


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


def _prepare_already_unstaged(repo: Path) -> None:
    (repo / "file.txt").write_text("first edit\n", encoding="utf-8")


def _mutate_unstaged(repo: Path) -> None:
    (repo / "file.txt").write_text("second edit\n", encoding="utf-8")


def _prepare_already_staged(repo: Path) -> None:
    (repo / "file.txt").write_text("first edit\n", encoding="utf-8")
    _git(["add", "file.txt"], cwd=repo)


def _mutate_staged(repo: Path) -> None:
    (repo / "file.txt").write_text("second edit\n", encoding="utf-8")
    _git(["add", "file.txt"], cwd=repo)


def _prepare_already_untracked(repo: Path) -> None:
    (repo / "extra.txt").write_text("first edit\n", encoding="utf-8")


def _mutate_untracked(repo: Path) -> None:
    (repo / "extra.txt").write_text("second edit\n", encoding="utf-8")


@pytest.mark.parametrize("role", [AgentRole.ARCHITECT, AgentRole.REVIEWER])
@pytest.mark.parametrize(
    ("prepare", "mutate"),
    [
        pytest.param(_prepare_already_unstaged, _mutate_unstaged, id="unstaged"),
        pytest.param(_prepare_already_staged, _mutate_staged, id="staged"),
        pytest.param(_prepare_already_untracked, _mutate_untracked, id="untracked"),
    ],
)
def test_ac_028_read_only_role_mutation_and_second_m_edit(
    tmp_path: Path,
    role: AgentRole,
    prepare: Callable[[Path], None],
    mutate: Callable[[Path], None],
) -> None:
    """Complements `test_check_git_state_read_only_role_delta_is_unsafe`
    above, which only proves the straightforward clean-to-`M` transition.
    AC-028's harder clause: a path already dirty (staged `M`, unstaged `M`,
    or untracked) at the `before` checkpoint, edited *again* to different
    content by a read-only role, with the porcelain classification staying
    byte-for-byte identical between `before` and `after` -- no new path
    enters `staged`/`unstaged`/`untracked`. Only `git-state-v1`'s
    content-sensitive fingerprint -- not porcelain-code parsing -- can
    catch a delta like this (see `git_safety.py`'s module docstring: "a
    second edit to an already-`M` file changes it").
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    prepare(repo)
    before = _check(target, sequence=0, purpose=f"{role.value}-attempt-1-before")

    mutate(repo)
    after = _check(
        target,
        sequence=1,
        purpose=f"{role.value}-attempt-1-after",
        role=role,
        baseline=before.state,
    )

    assert after.safety_status is GitSafetyStatus.UNSAFE
    # The porcelain-derived inventory is unchanged -- the delta is visible
    # only through the content fingerprint, never a new/changed path entry.
    assert after.state.staged == before.state.staged
    assert after.state.unstaged == before.state.unstaged
    assert after.state.untracked == before.state.untracked
    assert after.state.fingerprint != before.state.fingerprint


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


def test_check_git_state_probe_timeout_and_process_error_are_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complements `test_ac_036_git_probe_failure_is_indeterminate` in
    `tests/component/test_git_repository.py`, which proves AC-036's text at
    the `resolve_target`/preflight layer (a timeout, a non-zero exit, or
    ambiguous stdout on the initial `git rev-parse --show-toplevel` probe
    -> `PreflightError` `git_safety.top_level_probe_failed`). This proves
    the complementary checkpoint layer: a genuine git subprocess `TIMEOUT`
    or non-zero-exit `PROCESS_ERROR` on one of `capture_git_state`'s own
    six `_STATE_PROBES` (`git_safety.py`) is classified
    `GitSafetyStatus.INDETERMINATE` by `check_git_state`, never guessed
    `SAFE` -- the half of AC-036's text ("timeout o exit non-zero di un
    comando Git di safety") that only `capture_git_state`/`check_git_state`
    can exercise, and that every other `INDETERMINATE` test in this file
    triggers through an unrelated cause (a filesystem permission fault, or
    a monkeypatched return value) rather than a real probe failure.

    The target is resolved with the real `git` executable first (a real
    `TargetRepository` is required), and only the `check_git_state` call
    itself is redirected to `FAKE_GIT`
    (`tests/component/helpers/fake_git.py`), which ignores argv and
    responds identically to every subcommand -- forcing a failure on the
    first of the six probes (`branch`) is therefore sufficient to prove
    the classification for the checkpoint layer as a whole. Like the
    sibling AC-036 test, this asserts only the black-box
    `GitSafetyStatus` verdict: `capture_git_state`'s `except PreflightError`
    branch (`git_safety.py`) discards the failing probe's own
    `ProcessResult` entirely, so there is no `TIMEOUT`/`PROCESS_ERROR`
    outcome left on the returned record to assert on directly.
    """

    workspace = _workspace(tmp_path / "workspace")
    repo = _clean_repo(workspace.root / "repo")
    target = _resolve(workspace, repo)

    # A probe that exceeds the utility deadline fails closed.
    monkeypatch.setenv("FAKE_GIT_SLEEP_SECONDS", "5")
    timeout_record = _check(
        target,
        sequence=0,
        purpose="baseline",
        git_executable=FAKE_GIT,
        utility_timeout_seconds=0.2,
    )
    assert timeout_record.safety_status is GitSafetyStatus.INDETERMINATE
    monkeypatch.delenv("FAKE_GIT_SLEEP_SECONDS", raising=False)

    # A non-zero exit from a probe fails closed.
    monkeypatch.setenv("FAKE_GIT_EXIT_CODE", "128")
    nonzero_record = _check(
        target,
        sequence=1,
        purpose="baseline",
        git_executable=FAKE_GIT,
    )
    assert nonzero_record.safety_status is GitSafetyStatus.INDETERMINATE
    monkeypatch.delenv("FAKE_GIT_EXIT_CODE", raising=False)


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

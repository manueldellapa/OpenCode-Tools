"""Component coverage for the disposable coder Git sandbox (issue #90)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.coder_sandbox import (
    CoderSandbox,
    CoderSandboxError,
    cleanup_coder_sandbox,
    prepare_coder_sandbox,
    promote_coder_changes,
)
from opencode_tools.domain import TargetRepository
from opencode_tools.process import SubprocessRunner


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def _git(args: list[str], *, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, TargetRepository]:
    root = tmp_path / "target"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / "README.md").write_text("before\n", encoding="utf-8")
    (root / "REMOVE.txt").write_text("remove me\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], cwd=root))
    return root, TargetRepository(
        root=root.resolve(),
        workspace_relative=Path("."),
        git_common_dir=git_dir.resolve(),
    )


def _prepare(
    target: TargetRepository,
) -> tuple[Path, RealClock, SubprocessRunner, CoderSandbox]:
    git = shutil.which("git")
    assert git is not None
    clock = RealClock()
    runner = SubprocessRunner(clock)
    sandbox = prepare_coder_sandbox(
        runner,
        git_executable=Path(git),
        target=target,
        clock=clock,
        utility_timeout_seconds=10,
        termination_grace_seconds=1,
    )
    return Path(git), clock, runner, sandbox


def test_coder_sandbox_promotes_only_working_tree_delta(tmp_path: Path) -> None:
    root, target = _repository(tmp_path)
    original_head = _git(["rev-parse", "HEAD"], cwd=root)
    original_count = _git(["rev-list", "--count", "HEAD"], cwd=root)

    git, clock, runner, sandbox = _prepare(target)
    try:
        assert sandbox.root != root
        assert (sandbox.root / ".git").is_dir()
        assert _git(["remote"], cwd=sandbox.root) == ""
        assert not (sandbox.root / ".git/objects/info/alternates").exists()

        (sandbox.root / "README.md").write_text("after\n", encoding="utf-8")
        (sandbox.root / "REMOVE.txt").unlink()
        (sandbox.root / "NEW.txt").write_text("new\n", encoding="utf-8")

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "after\n"
    assert not (root / "REMOVE.txt").exists()
    assert (root / "NEW.txt").read_text(encoding="utf-8") == "new\n"
    assert _git(["rev-parse", "HEAD"], cwd=root) == original_head
    assert _git(["rev-list", "--count", "HEAD"], cwd=root) == original_count


def test_destroying_and_reinitializing_sandbox_git_cannot_damage_target(
    tmp_path: Path,
) -> None:
    root, target = _repository(tmp_path)
    original_head = _git(["rev-parse", "HEAD"], cwd=root)
    original_log = _git(["log", "--oneline", "--decorate"], cwd=root)

    git, clock, runner, sandbox = _prepare(target)
    try:
        shutil.rmtree(sandbox.root / ".git")
        _git(["init", "--quiet", "--initial-branch=main"], cwd=sandbox.root)
        (sandbox.root / "README.md").write_text("destroyed sandbox\n", encoding="utf-8")

        with pytest.raises(CoderSandboxError, match="sandbox Git metadata|HEAD"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / ".git").is_dir()
    assert _git(["rev-parse", "HEAD"], cwd=root) == original_head
    assert _git(["log", "--oneline", "--decorate"], cwd=root) == original_log
    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_target_drift_blocks_sandbox_promotion(tmp_path: Path) -> None:
    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        (root / "README.md").write_text("external change\n", encoding="utf-8")

        with pytest.raises(CoderSandboxError, match="real target changed"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "external change\n"


def test_promotion_refuses_and_never_runs_a_coder_planted_clean_filter(
    tmp_path: Path,
) -> None:
    """GH #110: a coder-declared clean filter must never execute during promotion."""

    root, target = _repository(tmp_path)
    marker = tmp_path / "pwned-marker"

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        (sandbox.root / ".gitattributes").write_text(
            "* filter=evil\n", encoding="utf-8"
        )
        _git(
            [
                "config",
                "--local",
                "filter.evil.clean",
                f"touch {marker} && cat",
            ],
            cwd=sandbox.root,
        )

        with pytest.raises(CoderSandboxError, match="gitattributes"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not marker.exists()
    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_promotion_refuses_a_coder_planted_git_config_change_alone(
    tmp_path: Path,
) -> None:
    """GH #110: a config-only driver (e.g. diff.external) must also block promotion."""

    root, target = _repository(tmp_path)
    marker = tmp_path / "pwned-marker-config-only"

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        _git(
            ["config", "--local", "diff.external", f"sh -c 'touch {marker}'"],
            cwd=sandbox.root,
        )

        with pytest.raises(CoderSandboxError, match="Git configuration"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not marker.exists()
    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_promotion_refuses_a_coder_planted_local_info_attributes(
    tmp_path: Path,
) -> None:
    """GH #110: an untracked .git/info/attributes override must also block promotion."""

    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        (sandbox.root / ".git" / "info" / "attributes").write_text(
            "* filter=evil\n", encoding="utf-8"
        )

        with pytest.raises(CoderSandboxError, match="attribute overrides"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"

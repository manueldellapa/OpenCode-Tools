"""Component tests for the M10-01 runtime location bootstrap: the real-`git`
`check-ignore` wiring behind `check_runtime_root_ignored`/
`check_runtime_location` (System Design SS15.1; ADR-008; FR-015, FR-042;
AC-022).

Builds real temporary Git repositories and drives these functions through
the real `SubprocessRunner` and a real `git` executable, never a fixture or
mock of `ProcessRunner` itself. Pure filesystem-walk coverage for
`classify_runtime_root_containment` (no subprocess involved) lives in
`tests/unit/test_runtime_bootstrap.py`.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.errors import PreflightError
from opencode_tools.git_safety import (
    check_runtime_location,
    check_runtime_root_ignored,
    resolve_git_executable,
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

GIT_EXECUTABLE = resolve_git_executable()


class RealClock:
    """A `Clock` reading genuine wall/monotonic time for a real subprocess."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True
    )


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    return root


def _check_location(runtime_root: Path) -> None:
    check_runtime_location(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        runtime_root=runtime_root,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


# --- outside any repository: accepted without an ignore probe -------------


def test_check_runtime_location_accepts_a_root_outside_any_repository(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "external" / ".opencode-tools"

    _check_location(runtime_root)

    assert not runtime_root.exists()


# --- under Git metadata: always rejected, no probe needed ------------------


def test_check_runtime_location_rejects_a_root_under_git_metadata(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = repo / ".git" / "opencode-tools"

    with pytest.raises(PreflightError) as exc_info:
        _check_location(runtime_root)

    assert exc_info.value.code == "git_safety.runtime_root_under_git_metadata"
    assert not runtime_root.exists()


# --- AC-022: fail-closed, non-mutating runtime ignore probe -----------------


def test_ac_022_runtime_ignore_is_fail_closed_and_non_mutating(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    runtime_root = repo / ".opencode-tools"

    with pytest.raises(PreflightError) as exc_info:
        _check_location(runtime_root)

    assert exc_info.value.code == "git_safety.runtime_root_not_ignored"
    assert not runtime_root.exists()
    assert not (repo / ".gitignore").exists()


def test_check_runtime_location_accepts_an_already_ignored_root_and_creates_nothing(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".opencode-tools/\n", encoding="utf-8")
    runtime_root = repo / ".opencode-tools"

    _check_location(runtime_root)

    assert not runtime_root.exists()


def test_check_runtime_location_accepts_a_bare_pattern_without_a_trailing_slash(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".opencode-tools\n", encoding="utf-8")
    runtime_root = repo / ".opencode-tools"

    _check_location(runtime_root)


def test_check_runtime_location_rejects_an_unrelated_ignore_rule(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("not-opencode-tools/\n", encoding="utf-8")
    runtime_root = repo / ".opencode-tools"

    with pytest.raises(PreflightError) as exc_info:
        _check_location(runtime_root)
    assert exc_info.value.code == "git_safety.runtime_root_not_ignored"


def test_check_runtime_location_accepts_the_root_already_existing_and_ignored(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".opencode-tools/\n", encoding="utf-8")
    runtime_root = repo / ".opencode-tools"
    runtime_root.mkdir()

    _check_location(runtime_root)


# --- check_runtime_root_ignored called directly, bypassing containment ----


def test_check_runtime_root_ignored_can_be_called_with_an_explicit_working_tree(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".opencode-tools/\n", encoding="utf-8")
    runtime_root = repo / ".opencode-tools"

    check_runtime_root_ignored(
        SubprocessRunner(RealClock()),
        git_executable=GIT_EXECUTABLE,
        working_tree_root=repo,
        runtime_root=runtime_root,
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


def test_check_runtime_root_ignored_rejects_a_sentinel_not_covered_by_the_rule(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    # Ignores only one specific, differently named file inside the runtime
    # root -- the directory itself is never matched, and neither is the
    # `.probe` sentinel, so this must still fail exactly like an entirely
    # missing rule would.
    (repo / ".gitignore").write_text(
        ".opencode-tools/only-this-file\n", encoding="utf-8"
    )
    runtime_root = repo / ".opencode-tools"

    with pytest.raises(PreflightError) as exc_info:
        check_runtime_root_ignored(
            SubprocessRunner(RealClock()),
            git_executable=GIT_EXECUTABLE,
            working_tree_root=repo,
            runtime_root=runtime_root,
            utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
            termination_grace_seconds=TERMINATION_GRACE_SECONDS,
        )
    assert exc_info.value.code == "git_safety.runtime_root_not_ignored"


# --- nested working tree: the nearest repository governs -------------------


def test_check_runtime_location_uses_the_nearest_containing_repository(
    tmp_path: Path,
) -> None:
    outer = _init_repo(tmp_path / "outer")
    (outer / ".gitignore").write_text(".opencode-tools/\n", encoding="utf-8")
    inner = _init_repo(outer / "inner")
    runtime_root = inner / ".opencode-tools"

    # The outer repository ignores it, but the inner (nearest) repository,
    # which the check must actually use, has no such rule.
    with pytest.raises(PreflightError) as exc_info:
        _check_location(runtime_root)
    assert exc_info.value.code == "git_safety.runtime_root_not_ignored"

"""Unit tests for the M10-01 runtime bootstrap: Git containment
classification and command construction in `git_safety.py`, and the
platform baseline gate and runtime-root creation/validation in
`runlog.py`.

Every test here is pure filesystem/logic: no subprocess is spawned and no
real Git executable is invoked. Component-level, real-`git`-subprocess
coverage for the ignore probe itself (`check_runtime_root_ignored`,
`check_runtime_location`) lives in `tests/component/test_runtime_location.py`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from opencode_tools.errors import PreflightError
from opencode_tools.git_safety import (
    build_check_ignore_argv,
    classify_runtime_root_containment,
)
from opencode_tools.runlog import (
    DIRECTORY_MODE,
    bootstrap_runtime_root,
    check_platform_baseline,
)

GIT_EXECUTABLE = Path("/usr/bin/git")

# --- classify_runtime_root_containment -----------------------------------------


def test_classify_runtime_root_containment_rejects_a_path_under_git_metadata(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    runtime_root = git_dir / "opencode-tools"

    with pytest.raises(PreflightError) as exc_info:
        classify_runtime_root_containment(runtime_root)
    assert exc_info.value.code == "git_safety.runtime_root_under_git_metadata"


def test_classify_runtime_root_containment_rejects_a_deeply_nested_metadata_path(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    runtime_root = git_dir / "objects" / "pack" / "deep" / "opencode-tools"

    with pytest.raises(PreflightError) as exc_info:
        classify_runtime_root_containment(runtime_root)
    assert exc_info.value.code == "git_safety.runtime_root_under_git_metadata"


def test_classify_runtime_root_containment_rejects_the_git_directory_itself(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)

    with pytest.raises(PreflightError) as exc_info:
        classify_runtime_root_containment(git_dir)
    assert exc_info.value.code == "git_safety.runtime_root_under_git_metadata"


def test_classify_runtime_root_containment_returns_the_working_tree_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    runtime_root = workspace / ".opencode-tools"

    assert classify_runtime_root_containment(runtime_root) == workspace


def test_classify_runtime_root_containment_treats_a_dot_git_file_as_a_marker(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    runtime_root = workspace / ".opencode-tools"

    assert classify_runtime_root_containment(runtime_root) == workspace


def test_classify_runtime_root_containment_returns_none_outside_any_repository(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "external" / ".opencode-tools"

    assert classify_runtime_root_containment(runtime_root) is None


def test_classify_runtime_root_containment_finds_the_nearest_working_tree(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (outer / ".git").mkdir(parents=True)
    (inner / ".git").mkdir(parents=True)
    runtime_root = inner / ".opencode-tools"

    assert classify_runtime_root_containment(runtime_root) == inner


def test_classify_runtime_root_containment_requires_an_absolute_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        classify_runtime_root_containment(Path("relative/.opencode-tools"))


# --- build_check_ignore_argv ----------------------------------------------------


def test_build_check_ignore_argv_constructs_the_fixed_shape(tmp_path: Path) -> None:
    working_tree_root = tmp_path / "repo"

    argv = build_check_ignore_argv(
        GIT_EXECUTABLE, working_tree_root, f"{tmp_path / 'repo' / '.opencode-tools'}/"
    )

    assert argv == (
        str(GIT_EXECUTABLE),
        "-C",
        str(working_tree_root),
        "check-ignore",
        "--quiet",
        "--no-index",
        "--",
        f"{tmp_path / 'repo' / '.opencode-tools'}/",
    )


# --- check_platform_baseline ----------------------------------------------------


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_check_platform_baseline_accepts_the_supported_platforms(
    platform: str,
) -> None:
    check_platform_baseline(platform=platform, os_name="posix")


def test_check_platform_baseline_rejects_an_unsupported_platform() -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_platform_baseline(platform="win32", os_name="nt")
    assert exc_info.value.code == "runlog.unsupported_platform"


def test_check_platform_baseline_rejects_a_non_posix_os_name() -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_platform_baseline(platform="linux", os_name="nt")
    assert exc_info.value.code == "runlog.unsupported_platform"


# --- bootstrap_runtime_root -------------------------------------------------


def test_bootstrap_runtime_root_creates_a_fresh_root_with_private_mode(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "fresh-root"

    bootstrap_runtime_root(runtime_root)

    assert runtime_root.is_dir()
    assert not runtime_root.is_symlink()
    assert stat.S_IMODE(runtime_root.stat().st_mode) == DIRECTORY_MODE


def test_bootstrap_runtime_root_accepts_an_already_valid_existing_root(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "existing-root"
    runtime_root.mkdir(mode=DIRECTORY_MODE)

    bootstrap_runtime_root(runtime_root)
    bootstrap_runtime_root(runtime_root)

    assert runtime_root.is_dir()


def test_bootstrap_runtime_root_rejects_a_symlinked_root_without_following_it(
    tmp_path: Path,
) -> None:
    real_target = tmp_path / "real-target"
    real_target.mkdir(mode=DIRECTORY_MODE)
    runtime_root = tmp_path / "symlinked-root"
    runtime_root.symlink_to(real_target, target_is_directory=True)

    with pytest.raises(PreflightError) as exc_info:
        bootstrap_runtime_root(runtime_root)

    assert exc_info.value.code == "runlog.runtime_root_is_symlink"
    assert runtime_root.is_symlink()


def test_bootstrap_runtime_root_rejects_a_file_at_the_root_path(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "plain-file"
    runtime_root.write_text("not a directory")

    with pytest.raises(PreflightError) as exc_info:
        bootstrap_runtime_root(runtime_root)

    assert exc_info.value.code == "runlog.runtime_root_not_a_directory"


def test_bootstrap_runtime_root_rejects_too_permissive_mode_without_fixing_it(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "loose-root"
    runtime_root.mkdir(mode=0o755)
    os.chmod(runtime_root, 0o755)

    with pytest.raises(PreflightError) as exc_info:
        bootstrap_runtime_root(runtime_root)

    assert exc_info.value.code == "runlog.runtime_root_mode_too_permissive"
    assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o755


def test_bootstrap_runtime_root_rejects_wrong_ownership_without_fixing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "someone-elses-root"
    runtime_root.mkdir(mode=DIRECTORY_MODE)
    real_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_uid + 1)

    with pytest.raises(PreflightError) as exc_info:
        bootstrap_runtime_root(runtime_root)

    assert exc_info.value.code == "runlog.runtime_root_wrong_owner"


def test_bootstrap_runtime_root_wraps_an_unexpected_create_failure(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "missing-parent" / "root"

    with pytest.raises(PreflightError) as exc_info:
        bootstrap_runtime_root(runtime_root)

    assert exc_info.value.code == "runlog.runtime_root_create_failed"
    assert not runtime_root.exists()


def test_bootstrap_runtime_root_wraps_an_unverifiable_existing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "unverifiable-root"
    runtime_root.mkdir(mode=DIRECTORY_MODE)

    def _raise(path: object) -> os.stat_result:
        raise OSError("simulated lstat failure")

    monkeypatch.setattr(os, "lstat", _raise)

    with pytest.raises(PreflightError) as exc_info:
        bootstrap_runtime_root(runtime_root)

    assert exc_info.value.code == "runlog.runtime_root_unverifiable"


def test_bootstrap_runtime_root_rejects_a_relative_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        bootstrap_runtime_root(Path("relative-root"))

"""Unit tests for Git command construction, allowlisting, and probe-output
parsing in `git_safety.py` (M09-01).

Every test here is pure: no subprocess is spawned and no real Git repository
is touched. Component-level, real-repository coverage for the full
`resolve_target` preflight (AC-004-AC-006, AC-036) lives in
`tests/component/test_git_repository.py`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from opencode_tools.errors import PreflightError
from opencode_tools.git_safety import (
    ALLOWED_GIT_ARGV_TAILS,
    build_git_argv,
    check_branch_attached,
    check_clean_worktree,
    check_git_argv_is_allowlisted,
    check_not_bare,
    check_top_level,
    parse_git_common_dir,
    resolve_git_executable,
)

# --- resolve_git_executable ----------------------------------------------------


def test_resolve_git_executable_returns_the_resolved_which_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(
        shutil, "which", lambda name: str(fake) if name == "git" else None
    )
    assert resolve_git_executable() == fake.resolve()


def test_resolve_git_executable_fails_closed_when_not_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(PreflightError) as exc_info:
        resolve_git_executable()
    assert exc_info.value.code == "git_safety.executable_not_found"


# --- check_git_argv_is_allowlisted / build_git_argv -----------------------------


@pytest.mark.parametrize("argv_tail", ALLOWED_GIT_ARGV_TAILS)
def test_check_git_argv_is_allowlisted_accepts_each_canonical_command(
    argv_tail: tuple[str, ...],
) -> None:
    check_git_argv_is_allowlisted(argv_tail)


@pytest.mark.parametrize(
    "argv_tail",
    [
        ("commit", "-m", "hack"),
        ("push",),
        ("reset", "--hard"),
        ("clean", "-fd"),
        ("stash",),
        ("checkout", "."),
        ("rev-parse", "--show-toplevel", "--extra-flag"),
        (),
    ],
)
def test_check_git_argv_is_allowlisted_rejects_anything_else(
    argv_tail: tuple[str, ...],
) -> None:
    with pytest.raises(AssertionError):
        check_git_argv_is_allowlisted(argv_tail)


def test_build_git_argv_produces_c_target_then_the_allowlisted_tail(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "git"
    target = tmp_path / "target"

    argv = build_git_argv(executable, target, ("rev-parse", "--show-toplevel"))

    assert argv == (
        str(executable),
        "-C",
        str(target),
        "rev-parse",
        "--show-toplevel",
    )


def test_build_git_argv_rejects_a_non_allowlisted_tail(tmp_path: Path) -> None:
    with pytest.raises(AssertionError):
        build_git_argv(tmp_path / "git", tmp_path / "target", ("push",))


# --- check_top_level -------------------------------------------------------------


def test_check_top_level_accepts_an_exact_match(tmp_path: Path) -> None:
    check_top_level(f"{tmp_path}\n", tmp_path)


@pytest.mark.parametrize(
    "raw_output_suffix",
    ["/nested", "/sibling"],
)
def test_check_top_level_rejects_any_mismatch(
    tmp_path: Path, raw_output_suffix: str
) -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_top_level(f"{tmp_path}{raw_output_suffix}\n", tmp_path)
    assert exc_info.value.code == "git_safety.not_git_top_level"


def test_check_top_level_rejects_empty_output() -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_top_level("", Path("/some/target"))
    assert exc_info.value.code == "git_safety.not_git_top_level"


# --- check_not_bare ----------------------------------------------------------------


def test_check_not_bare_accepts_false() -> None:
    check_not_bare("false\n")


def test_check_not_bare_rejects_true() -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_not_bare("true\n")
    assert exc_info.value.code == "git_safety.bare_repository"


# --- parse_git_common_dir --------------------------------------------------------


def test_parse_git_common_dir_strips_the_trailing_newline(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    assert parse_git_common_dir(f"{git_dir}\n") == git_dir


def test_parse_git_common_dir_rejects_empty_output() -> None:
    with pytest.raises(PreflightError) as exc_info:
        parse_git_common_dir("")
    assert exc_info.value.code == "git_safety.git_dir_probe_failed"


# --- check_branch_attached --------------------------------------------------------


def test_check_branch_attached_returns_the_branch_name() -> None:
    assert check_branch_attached("main\n") == "main"


def test_check_branch_attached_rejects_empty_output_as_detached_head() -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_branch_attached("")
    assert exc_info.value.code == "git_safety.detached_head"


# --- check_clean_worktree ----------------------------------------------------------


def test_check_clean_worktree_accepts_empty_output() -> None:
    check_clean_worktree("")


@pytest.mark.parametrize(
    "raw_output",
    [
        " M staged-or-unstaged.py\x00",
        "?? untracked.py\x00",
        "A  new-file.py\x00",
    ],
)
def test_check_clean_worktree_rejects_any_porcelain_entry(raw_output: str) -> None:
    with pytest.raises(PreflightError) as exc_info:
        check_clean_worktree(raw_output)
    assert exc_info.value.code == "git_safety.dirty_worktree"

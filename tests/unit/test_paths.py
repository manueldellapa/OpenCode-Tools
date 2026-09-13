"""Unit tests for CLI-facing run-request path resolution and validation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from opencode_tools.config import (
    build_run_request,
    resolve_target_root,
    resolve_workspace,
)
from opencode_tools.domain import RunRequest, Workspace
from opencode_tools.errors import ConfigError


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


# --- resolve_workspace ------------------------------------------------------


def test_resolve_workspace_accepts_an_absolute_existing_directory(
    tmp_path: Path,
) -> None:
    workspace_dir = _make_workspace(tmp_path)

    workspace = resolve_workspace(workspace_dir, cwd=tmp_path)

    assert workspace == Workspace(root=workspace_dir.resolve())


def test_resolve_workspace_resolves_a_relative_path_against_cwd(
    tmp_path: Path,
) -> None:
    _make_workspace(tmp_path)

    workspace = resolve_workspace(Path("workspace"), cwd=tmp_path)

    assert workspace.root == (tmp_path / "workspace").resolve()


def test_resolve_workspace_canonicalizes_a_symlinked_workspace(
    tmp_path: Path,
) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir, target_is_directory=True)

    workspace = resolve_workspace(link, cwd=tmp_path)

    assert workspace.root == real_dir.resolve()


def test_resolve_workspace_rejects_a_missing_workspace(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ConfigError) as exc_info:
        resolve_workspace(missing, cwd=tmp_path)

    assert exc_info.value.code == "config.workspace_not_found"


def test_resolve_workspace_rejects_a_file_as_workspace(tmp_path: Path) -> None:
    file_path = tmp_path / "workspace.txt"
    file_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        resolve_workspace(file_path, cwd=tmp_path)

    assert exc_info.value.code == "config.workspace_not_directory"


# --- resolve_target_root -----------------------------------------------------


def test_resolve_target_root_accepts_dot_as_the_workspace_itself(
    tmp_path: Path,
) -> None:
    workspace_dir = _make_workspace(tmp_path)
    workspace = resolve_workspace(workspace_dir, cwd=tmp_path)

    target_root = resolve_target_root(Path("."), workspace=workspace)

    assert target_root == workspace.root


def test_resolve_target_root_accepts_a_relative_subdirectory(
    tmp_path: Path,
) -> None:
    workspace_dir = _make_workspace(tmp_path)
    (workspace_dir / "backend").mkdir()
    workspace = resolve_workspace(workspace_dir, cwd=tmp_path)

    target_root = resolve_target_root(Path("backend"), workspace=workspace)

    assert target_root == (workspace_dir / "backend").resolve()


def test_resolve_target_root_accepts_a_nested_relative_subdirectory(
    tmp_path: Path,
) -> None:
    workspace_dir = _make_workspace(tmp_path)
    (workspace_dir / "a" / "b").mkdir(parents=True)
    workspace = resolve_workspace(workspace_dir, cwd=tmp_path)

    target_root = resolve_target_root(Path("a/b"), workspace=workspace)

    assert target_root == (workspace_dir / "a" / "b").resolve()


def test_resolve_target_root_rejects_an_absolute_target(tmp_path: Path) -> None:
    workspace_dir = _make_workspace(tmp_path)
    workspace = resolve_workspace(workspace_dir, cwd=tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        resolve_target_root(tmp_path / "elsewhere", workspace=workspace)

    assert exc_info.value.code == "config.target_not_relative"


def test_resolve_target_root_rejects_a_traversal_target(tmp_path: Path) -> None:
    workspace_dir = _make_workspace(tmp_path)
    workspace = resolve_workspace(workspace_dir, cwd=tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        resolve_target_root(Path("../escape"), workspace=workspace)

    assert exc_info.value.code == "config.target_traversal"


def test_resolve_target_root_rejects_a_nested_traversal_target(
    tmp_path: Path,
) -> None:
    workspace_dir = _make_workspace(tmp_path)
    (workspace_dir / "backend").mkdir()
    workspace = resolve_workspace(workspace_dir, cwd=tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        resolve_target_root(Path("backend/../backend"), workspace=workspace)

    assert exc_info.value.code == "config.target_traversal"


def test_resolve_target_root_rejects_a_missing_target(tmp_path: Path) -> None:
    workspace_dir = _make_workspace(tmp_path)
    workspace = resolve_workspace(workspace_dir, cwd=tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        resolve_target_root(Path("missing"), workspace=workspace)

    assert exc_info.value.code == "config.target_not_found"


def test_resolve_target_root_rejects_a_symlink_escaping_the_workspace(
    tmp_path: Path,
) -> None:
    workspace_dir = _make_workspace(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    link = workspace_dir / "escape"
    link.symlink_to(outside_dir, target_is_directory=True)
    workspace = resolve_workspace(workspace_dir, cwd=tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        resolve_target_root(Path("escape"), workspace=workspace)

    assert exc_info.value.code == "config.target_escapes_workspace"


# --- build_run_request -------------------------------------------------------


def test_build_run_request_accepts_target_dot(tmp_path: Path) -> None:
    workspace_dir = _make_workspace(tmp_path)

    request = build_run_request(
        issue_number=4,
        workspace=workspace_dir,
        target=Path("."),
        cwd=tmp_path,
    )

    assert request == RunRequest(
        issue_number=4,
        workspace=Workspace(root=workspace_dir.resolve()),
        target_root=workspace_dir.resolve(),
    )


def test_build_run_request_accepts_a_relative_target(tmp_path: Path) -> None:
    workspace_dir = _make_workspace(tmp_path)
    (workspace_dir / "backend").mkdir()

    request = build_run_request(
        issue_number=4,
        workspace=workspace_dir,
        target=Path("backend"),
        cwd=tmp_path,
    )

    assert request.target_root == (workspace_dir / "backend").resolve()
    assert request.target_root.is_relative_to(request.workspace.root)


def test_build_run_request_produces_stable_canonical_paths(tmp_path: Path) -> None:
    workspace_dir = _make_workspace(tmp_path)
    (workspace_dir / "backend").mkdir()

    first = build_run_request(
        issue_number=4,
        workspace=workspace_dir,
        target=Path("backend"),
        cwd=tmp_path,
    )
    second = build_run_request(
        issue_number=4,
        workspace=workspace_dir,
        target=Path("backend"),
        cwd=tmp_path,
    )

    assert first == second


@pytest.mark.parametrize(
    "invalid_issue_number",
    [None, -1, 0, True, False, "4", 4.0, [4], {"issue": 4}],
)
def test_build_run_request_rejects_invalid_issue_numbers(
    tmp_path: Path,
    invalid_issue_number: object,
) -> None:
    workspace_dir = _make_workspace(tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        build_run_request(
            issue_number=cast(int, invalid_issue_number),
            workspace=workspace_dir,
            target=Path("."),
            cwd=tmp_path,
        )

    assert exc_info.value.code == "config.issue_number_invalid"


def test_build_run_request_propagates_workspace_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        build_run_request(
            issue_number=4,
            workspace=tmp_path / "missing",
            target=Path("."),
            cwd=tmp_path,
        )

    assert exc_info.value.code == "config.workspace_not_found"


def test_build_run_request_propagates_target_errors(tmp_path: Path) -> None:
    workspace_dir = _make_workspace(tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        build_run_request(
            issue_number=4,
            workspace=workspace_dir,
            target=tmp_path / "outside",
            cwd=tmp_path,
        )

    assert exc_info.value.code == "config.target_not_relative"

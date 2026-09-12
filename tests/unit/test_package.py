"""Tests for the package metadata and dependency manifest."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final, cast

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
PYPROJECT_PATH: Final = PROJECT_ROOT / "pyproject.toml"
SOURCE_ROOT: Final = PROJECT_ROOT / "src"


def _load_manifest() -> dict[str, object]:
    with PYPROJECT_PATH.open("rb") as manifest_file:
        return cast(dict[str, object], tomllib.load(manifest_file))


def _table(manifest: dict[str, object], name: str) -> dict[str, object]:
    value = manifest[name]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_package_imports_from_src_and_exposes_manifest_version() -> None:
    project = _table(_load_manifest(), "project")
    expected_version = project["version"]
    assert project["name"] == "opencode-tools"
    assert isinstance(expected_version, str)
    assert expected_version

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import opencode_tools; "
                "print(opencode_tools.__version__); "
                "print(opencode_tools.__file__)"
            ),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    imported_version, imported_path = completed.stdout.splitlines()
    assert imported_version == expected_version
    assert Path(imported_path).resolve() == SOURCE_ROOT / "opencode_tools/__init__.py"


def test_manifest_requires_supported_python() -> None:
    project = _table(_load_manifest(), "project")

    assert project["requires-python"] == ">=3.13"
    assert sys.version_info >= (3, 13)


def test_manifest_has_only_the_declared_development_tools() -> None:
    manifest = _load_manifest()
    project = _table(manifest, "project")
    dependency_groups = _table(manifest, "dependency-groups")

    assert project["dependencies"] == []
    assert "optional-dependencies" not in project
    assert set(dependency_groups) == {"dev"}
    assert dependency_groups["dev"] == ["pytest", "ruff", "mypy"]

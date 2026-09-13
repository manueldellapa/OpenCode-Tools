"""Unit tests for config-file discovery, precedence, and closed-schema shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencode_tools.config import (
    CONVENTIONAL_CONFIG_FILENAME,
    RawConfig,
    load_raw_config,
    read_config_file,
    resolve_config_source,
)
from opencode_tools.domain import Workspace
from opencode_tools.errors import ConfigError

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def _fixture(name: str) -> Path:
    return FIXTURES_ROOT / name


def _make_workspace(tmp_path: Path) -> Workspace:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    return Workspace(root=workspace_dir.resolve())


# --- resolve_config_source: discovery and precedence ------------------------


def test_resolve_config_source_uses_defaults_when_nothing_is_given(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)

    source, path = resolve_config_source(
        config_path=None,
        workspace=workspace,
        cwd=tmp_path,
    )

    assert (source, path) == ("defaults", None)


def test_resolve_config_source_uses_the_conventional_file_when_present(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    conventional = workspace.root / CONVENTIONAL_CONFIG_FILENAME
    conventional.write_text("version = 1\n", encoding="utf-8")

    source, path = resolve_config_source(
        config_path=None,
        workspace=workspace,
        cwd=tmp_path,
    )

    assert source == "conventional"
    assert path == conventional.resolve()


def test_resolve_config_source_explicit_wins_over_conventional(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace.root / CONVENTIONAL_CONFIG_FILENAME).write_text(
        "version = 1\n", encoding="utf-8"
    )
    explicit = tmp_path / "explicit.toml"
    explicit.write_text("version = 1\n", encoding="utf-8")

    source, path = resolve_config_source(
        config_path=explicit,
        workspace=workspace,
        cwd=tmp_path,
    )

    assert source == "explicit"
    assert path == explicit.resolve()


def test_resolve_config_source_resolves_a_relative_explicit_path_against_cwd(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    explicit = tmp_path / "explicit.toml"
    explicit.write_text("version = 1\n", encoding="utf-8")

    source, path = resolve_config_source(
        config_path=Path("explicit.toml"),
        workspace=workspace,
        cwd=tmp_path,
    )

    assert source == "explicit"
    assert path == explicit.resolve()


def test_resolve_config_source_rejects_a_missing_explicit_file(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        resolve_config_source(
            config_path=tmp_path / "missing.toml",
            workspace=workspace,
            cwd=tmp_path,
        )

    assert exc_info.value.code == "config.file_not_found"


def test_resolve_config_source_rejects_a_directory_as_explicit_config(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    directory = tmp_path / "a-directory.toml"
    directory.mkdir()

    with pytest.raises(ConfigError) as exc_info:
        resolve_config_source(
            config_path=directory,
            workspace=workspace,
            cwd=tmp_path,
        )

    assert exc_info.value.code == "config.file_not_a_file"


def test_resolve_config_source_falls_back_to_defaults_without_conventional_file(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace.root / "not-the-conventional-name.toml").write_text(
        "version = 1\n", encoding="utf-8"
    )

    source, path = resolve_config_source(
        config_path=None,
        workspace=workspace,
        cwd=tmp_path,
    )

    assert (source, path) == ("defaults", None)


# --- read_config_file: parsing, version, and closed schema -------------------


def test_read_config_file_accepts_the_minimal_golden_fixture() -> None:
    data = read_config_file(_fixture("golden-minimal.toml"))

    assert data == {"version": 1}


def test_read_config_file_accepts_the_full_golden_fixture() -> None:
    data = read_config_file(_fixture("golden-full.toml"))

    assert data["version"] == 1
    assert data["execution"]["opencode_timeout_seconds"] == 1800  # type: ignore[index]
    assert data["provider_retry"]["multiplier"] == 2.0  # type: ignore[index]
    assert data["runtime"]["root"] == ".opencode-tools"  # type: ignore[index]
    assert data["github"]["targets"]["Backend"]["remote"] == "origin"  # type: ignore[index]


@pytest.mark.parametrize(
    ("fixture_name", "expected_code"),
    [
        ("missing-version.toml", "config.version_missing"),
        ("wrong-version.toml", "config.version_unsupported"),
        ("bool-version.toml", "config.version_invalid_type"),
        ("unknown-top-level-key.toml", "config.unknown_key"),
        ("unknown-execution-key.toml", "config.unknown_key"),
        ("unknown-target-entry-key.toml", "config.unknown_key"),
        ("duplicate-key.toml", "config.file_invalid_toml"),
        ("invalid-syntax.toml", "config.file_invalid_toml"),
        ("section-not-table.toml", "config.section_not_a_table"),
    ],
)
def test_read_config_file_rejects_invalid_fixtures(
    fixture_name: str,
    expected_code: str,
) -> None:
    with pytest.raises(ConfigError) as exc_info:
        read_config_file(_fixture(fixture_name))

    assert exc_info.value.code == expected_code


def test_read_config_file_rejects_an_unreadable_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"

    with pytest.raises(ConfigError) as exc_info:
        read_config_file(missing)

    assert exc_info.value.code == "config.file_unreadable"


# --- load_raw_config: end-to-end precedence and reproducibility -------------


def test_load_raw_config_uses_defaults_with_empty_data(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)

    raw = load_raw_config(config_path=None, workspace=workspace, cwd=tmp_path)

    assert raw == RawConfig(source="defaults", path=None, data={})


def test_load_raw_config_uses_the_conventional_file(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    conventional = workspace.root / CONVENTIONAL_CONFIG_FILENAME
    conventional.write_text("version = 1\n", encoding="utf-8")

    raw = load_raw_config(config_path=None, workspace=workspace, cwd=tmp_path)

    assert raw.source == "conventional"
    assert raw.path == conventional.resolve()
    assert raw.data == {"version": 1}


def test_load_raw_config_uses_an_explicit_file_over_the_conventional_one(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace.root / CONVENTIONAL_CONFIG_FILENAME).write_text(
        "version = 1\n", encoding="utf-8"
    )
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('version = 1\n[runtime]\nroot = "custom"\n', encoding="utf-8")

    raw = load_raw_config(config_path=explicit, workspace=workspace, cwd=tmp_path)

    assert raw.source == "explicit"
    assert raw.path == explicit.resolve()
    assert raw.data == {"version": 1, "runtime": {"root": "custom"}}


def test_load_raw_config_propagates_schema_errors(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace.root / CONVENTIONAL_CONFIG_FILENAME).write_text(
        'version = 1\nmodel = "gpt-unexpected"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError) as exc_info:
        load_raw_config(config_path=None, workspace=workspace, cwd=tmp_path)

    assert exc_info.value.code == "config.unknown_key"


def test_load_raw_config_is_reproducible_for_the_same_inputs(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    conventional = workspace.root / CONVENTIONAL_CONFIG_FILENAME
    conventional.write_text("version = 1\n", encoding="utf-8")

    first = load_raw_config(config_path=None, workspace=workspace, cwd=tmp_path)
    second = load_raw_config(config_path=None, workspace=workspace, cwd=tmp_path)

    assert first == second

"""Unit tests for config discovery, precedence, schema shape, and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencode_tools.config import (
    CONVENTIONAL_CONFIG_FILENAME,
    RawConfig,
    build_app_config,
    load_app_config,
    load_raw_config,
    read_config_file,
    resolve_config_source,
    sanitize_app_config,
)
from opencode_tools.domain import (
    AppConfig,
    ConfigSource,
    GithubTargetOverride,
    Workspace,
)
from opencode_tools.errors import ConfigError

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def _fixture(name: str) -> Path:
    return FIXTURES_ROOT / name


def _make_workspace(tmp_path: Path) -> Workspace:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    return Workspace(root=workspace_dir.resolve())


def _raw(data: dict[str, object], *, source: str = "defaults") -> RawConfig:
    return RawConfig(source=source, path=None, data=data)


FAKE_WORKSPACE = Workspace(root=Path("/opencode-tools-test-workspace"))


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


# --- build_app_config / load_app_config: defaults, ranges, cross-field -----


def test_build_app_config_applies_all_v1_defaults() -> None:
    config = build_app_config(_raw({"version": 1}), workspace=FAKE_WORKSPACE)

    assert config.source is ConfigSource.DEFAULTS
    assert config.execution.opencode_timeout_seconds == 1800
    assert config.execution.utility_timeout_seconds == 30
    assert config.execution.termination_grace_seconds == 5
    assert config.execution.max_review_cycles == 3
    assert config.provider_retry.max_attempts == 3
    assert config.provider_retry.initial_delay_seconds == 2
    assert config.provider_retry.multiplier == 2.0
    assert config.provider_retry.max_delay_seconds == 30
    assert config.runtime_root == FAKE_WORKSPACE.root / ".opencode-tools"
    assert config.github_targets == ()


def test_build_app_config_uses_explicit_values_from_data() -> None:
    data: dict[str, object] = {
        "version": 1,
        "execution": {
            "opencode_timeout_seconds": 900,
            "utility_timeout_seconds": 15,
            "termination_grace_seconds": 2,
            "max_review_cycles": 5,
        },
        "provider_retry": {
            "max_attempts": 4,
            "initial_delay_seconds": 1,
            "multiplier": 3.0,
            "max_delay_seconds": 60,
        },
        "runtime": {"root": "custom-runtime"},
    }

    config = build_app_config(_raw(data, source="explicit"), workspace=FAKE_WORKSPACE)

    assert config.source is ConfigSource.EXPLICIT
    assert config.execution.opencode_timeout_seconds == 900
    assert config.execution.max_review_cycles == 5
    assert config.provider_retry.max_attempts == 4
    assert config.provider_retry.multiplier == 3.0
    assert config.runtime_root == FAKE_WORKSPACE.root / "custom-runtime"


def test_load_app_config_applies_conventional_overrides_over_defaults(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    conventional = workspace.root / CONVENTIONAL_CONFIG_FILENAME
    conventional.write_text(
        "version = 1\n[execution]\nmax_review_cycles = 7\n",
        encoding="utf-8",
    )

    config = load_app_config(config_path=None, workspace=workspace, cwd=tmp_path)

    assert isinstance(config, AppConfig)
    assert config.source is ConfigSource.CONVENTIONAL
    assert config.execution.max_review_cycles == 7
    assert config.execution.opencode_timeout_seconds == 1800


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("execution", "opencode_timeout_seconds", True),
        ("execution", "max_review_cycles", 3.0),
        ("provider_retry", "max_attempts", 3.0),
        ("provider_retry", "multiplier", True),
    ],
)
def test_build_app_config_rejects_wrong_types(
    section: str,
    key: str,
    value: object,
) -> None:
    data: dict[str, object] = {"version": 1, section: {key: value}}

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_type"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("execution", "opencode_timeout_seconds", 0),
        ("execution", "opencode_timeout_seconds", 7201),
        ("execution", "opencode_timeout_seconds", float("nan")),
        ("execution", "opencode_timeout_seconds", float("inf")),
        ("execution", "utility_timeout_seconds", 0),
        ("execution", "utility_timeout_seconds", 301),
        ("execution", "termination_grace_seconds", 0.05),
        ("execution", "termination_grace_seconds", 60.1),
        ("execution", "max_review_cycles", 0),
        ("execution", "max_review_cycles", 21),
        ("provider_retry", "max_attempts", 0),
        ("provider_retry", "max_attempts", 11),
        ("provider_retry", "initial_delay_seconds", 0.05),
        ("provider_retry", "initial_delay_seconds", 300.1),
        ("provider_retry", "multiplier", 1.0),
        ("provider_retry", "multiplier", 10.0001),
    ],
)
def test_build_app_config_rejects_out_of_range_values(
    section: str,
    key: str,
    value: object,
) -> None:
    data: dict[str, object] = {"version": 1, section: {key: value}}

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_value"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("execution", "opencode_timeout_seconds", 1),
        ("execution", "opencode_timeout_seconds", 7200),
        ("execution", "max_review_cycles", 1),
        ("execution", "max_review_cycles", 20),
        ("provider_retry", "max_attempts", 1),
        ("provider_retry", "max_attempts", 10),
        ("provider_retry", "multiplier", 10.0),
    ],
)
def test_build_app_config_accepts_boundary_values(
    section: str,
    key: str,
    value: object,
) -> None:
    data: dict[str, object] = {"version": 1, section: {key: value}}

    config = build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert getattr(getattr(config, section), key) == value


def test_build_app_config_rejects_max_delay_below_initial_delay() -> None:
    data: dict[str, object] = {
        "version": 1,
        "provider_retry": {"initial_delay_seconds": 10, "max_delay_seconds": 5},
    }

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_value"


def test_build_app_config_accepts_max_delay_equal_to_initial_delay() -> None:
    data: dict[str, object] = {
        "version": 1,
        "provider_retry": {"initial_delay_seconds": 10, "max_delay_seconds": 10},
    }

    config = build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert config.provider_retry.max_delay_seconds == 10


def test_build_app_config_rejects_max_delay_above_1800() -> None:
    data: dict[str, object] = {
        "version": 1,
        "provider_retry": {"initial_delay_seconds": 0.1, "max_delay_seconds": 1800.1},
    }

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_value"


def test_build_app_config_rejects_an_empty_runtime_root() -> None:
    data: dict[str, object] = {"version": 1, "runtime": {"root": "   "}}

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_value"


def test_build_app_config_rejects_a_non_string_runtime_root() -> None:
    data: dict[str, object] = {"version": 1, "runtime": {"root": 5}}

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_type"


# --- runtime.root: canonicalization and Git-metadata rejection -------------


def test_build_app_config_resolves_a_relative_runtime_root_against_workspace() -> None:
    data: dict[str, object] = {"version": 1, "runtime": {"root": "custom"}}

    config = build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert config.runtime_root == FAKE_WORKSPACE.root / "custom"


def test_build_app_config_keeps_an_absolute_runtime_root_as_is() -> None:
    data: dict[str, object] = {
        "version": 1,
        "runtime": {"root": "/elsewhere/runtime"},
    }

    config = build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert config.runtime_root == Path("/elsewhere/runtime")


def test_build_app_config_canonicalizes_a_symlinked_runtime_root(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    real_dir = tmp_path / "real-runtime"
    real_dir.mkdir()
    link = workspace.root / "linked-runtime"
    link.symlink_to(real_dir, target_is_directory=True)
    data: dict[str, object] = {"version": 1, "runtime": {"root": "linked-runtime"}}

    config = build_app_config(_raw(data), workspace=workspace)

    assert config.runtime_root == real_dir.resolve()


def test_build_app_config_does_not_require_the_runtime_root_to_exist() -> None:
    data: dict[str, object] = {"version": 1, "runtime": {"root": "not-created-yet"}}

    config = build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert config.runtime_root == FAKE_WORKSPACE.root / "not-created-yet"


def test_build_app_config_rejects_a_runtime_root_under_git_metadata() -> None:
    data: dict[str, object] = {"version": 1, "runtime": {"root": ".git/runtime"}}

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_value"


# --- github.targets: remote/repository and duplicate-after-canonicalization -


def test_build_app_config_accepts_valid_github_target_overrides() -> None:
    data: dict[str, object] = {
        "version": 1,
        "github": {
            "targets": {
                "Backend": {
                    "remote": "origin",
                    "repository": "github.com/example/backend",
                },
                "frontend": {"remote": "origin"},
            }
        },
    }

    config = build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert set(config.github_targets) == {
        GithubTargetOverride(
            workspace_relative=Path("Backend"),
            remote="origin",
            repository="github.com/example/backend",
        ),
        GithubTargetOverride(workspace_relative=Path("frontend"), remote="origin"),
    }


def test_build_app_config_rejects_a_target_missing_remote_and_repository() -> None:
    data: dict[str, object] = {
        "version": 1,
        "github": {"targets": {"backend": {}}},
    }

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_value"


def test_build_app_config_rejects_an_invalid_repository_format() -> None:
    data: dict[str, object] = {
        "version": 1,
        "github": {"targets": {"backend": {"repository": "backend"}}},
    }

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_value"


def test_build_app_config_rejects_a_remote_with_control_characters() -> None:
    data: dict[str, object] = {
        "version": 1,
        "github": {"targets": {"backend": {"remote": "ori\x01gin"}}},
    }

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_value"


def test_build_app_config_rejects_duplicate_targets_after_canonicalization() -> None:
    data: dict[str, object] = {
        "version": 1,
        "github": {
            "targets": {
                "backend": {"remote": "origin"},
                "./backend": {"remote": "upstream"},
            }
        },
    }

    with pytest.raises(ConfigError) as exc_info:
        build_app_config(_raw(data), workspace=FAKE_WORKSPACE)

    assert exc_info.value.code == "config.field_invalid_value"


# --- sanitize_app_config: JSON-safe, source-carrying, secret-free ----------


def test_sanitize_app_config_produces_a_plain_json_safe_mapping() -> None:
    data: dict[str, object] = {
        "version": 1,
        "github": {"targets": {"backend": {"remote": "origin"}}},
    }
    config = build_app_config(_raw(data, source="explicit"), workspace=FAKE_WORKSPACE)

    sanitized = sanitize_app_config(config)

    assert sanitized["source"] == "EXPLICIT"
    assert sanitized["runtime_root"] == str(FAKE_WORKSPACE.root / ".opencode-tools")
    execution = sanitized["execution"]
    assert isinstance(execution, dict)
    assert execution["opencode_timeout_seconds"] == 1800
    provider_retry = sanitized["provider_retry"]
    assert isinstance(provider_retry, dict)
    assert provider_retry["max_attempts"] == 3
    assert sanitized["github_targets"] == (
        {"workspace_relative": "backend", "remote": "origin", "repository": None},
    )
    json.dumps(sanitized)


# --- load_app_config: end-to-end with runtime.root and github.targets ------


def test_load_app_config_resolves_runtime_root_and_github_targets(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    conventional = workspace.root / CONVENTIONAL_CONFIG_FILENAME
    conventional.write_text(
        "version = 1\n"
        "[runtime]\n"
        'root = "custom-runtime"\n'
        '[github.targets."Backend"]\n'
        'remote = "origin"\n'
        'repository = "github.com/example/backend"\n',
        encoding="utf-8",
    )

    config = load_app_config(config_path=None, workspace=workspace, cwd=tmp_path)

    assert config.runtime_root == (workspace.root / "custom-runtime").resolve()
    assert config.github_targets == (
        GithubTargetOverride(
            workspace_relative=Path("Backend"),
            remote="origin",
            repository="github.com/example/backend",
        ),
    )


# --- AC-002: effective-config precedence, determinism, and rejection --------


def test_ac_002_effective_config_precedence_and_rejection(tmp_path: Path) -> None:
    """Prove the full three-tier precedence through `load_app_config` itself.

    Explicit overrides conventional overrides defaults, deterministically,
    and invalid input never yields an `AppConfig` -- the config-layer
    precondition for the CLI never starting agents on a broken config.
    """
    workspace = _make_workspace(tmp_path)

    # (1) Neither conventional nor explicit file present: defaults apply.
    defaults_config = load_app_config(
        config_path=None, workspace=workspace, cwd=tmp_path
    )

    assert defaults_config.source is ConfigSource.DEFAULTS
    assert defaults_config.execution.max_review_cycles == 3

    # (2) Conventional file present, no explicit path: conventional overrides
    # the default.
    conventional = workspace.root / CONVENTIONAL_CONFIG_FILENAME
    conventional.write_text(
        "version = 1\n[execution]\nmax_review_cycles = 7\n",
        encoding="utf-8",
    )

    conventional_config = load_app_config(
        config_path=None, workspace=workspace, cwd=tmp_path
    )

    assert conventional_config.source is ConfigSource.CONVENTIONAL
    assert conventional_config.execution.max_review_cycles == 7

    # (3) An explicit path is also given, with a different override for the
    # same field: explicit wins over both the conventional file and the
    # default.
    explicit = tmp_path / "explicit.toml"
    explicit.write_text(
        "version = 1\n[execution]\nmax_review_cycles = 11\n",
        encoding="utf-8",
    )

    explicit_config = load_app_config(
        config_path=explicit, workspace=workspace, cwd=tmp_path
    )

    assert explicit_config.source is ConfigSource.EXPLICIT
    assert explicit_config.execution.max_review_cycles == 11

    # (4) Determinism: the same inputs produce an equal `AppConfig` again.
    explicit_config_again = load_app_config(
        config_path=explicit, workspace=workspace, cwd=tmp_path
    )

    assert explicit_config_again == explicit_config

    # (5) Rejection: invalid input never produces an `AppConfig`, whether it
    # comes from the conventional file or an explicit one.
    conventional.write_text('version = 1\nmodel = "gpt-unexpected"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_app_config(config_path=None, workspace=workspace, cwd=tmp_path)

    assert exc_info.value.code == "config.unknown_key"

    invalid_explicit = _fixture("unknown-top-level-key.toml")

    with pytest.raises(ConfigError) as exc_info:
        load_app_config(config_path=invalid_explicit, workspace=workspace, cwd=tmp_path)

    assert exc_info.value.code == "config.unknown_key"

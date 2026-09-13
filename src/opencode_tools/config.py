"""Path resolution and config validation, proven before any agent I/O.

Covers the workspace/target/issue-number shape of a `RunRequest`, and the
discovery/parsing/closed-schema/range validation that produces `AppConfig`.
Runtime-root canonicalization, Git-metadata rejection, GitHub target-override
validation, Git top-level proof, clean-baseline checks, and artifact creation
remain the responsibility of later milestones.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from opencode_tools.domain import (
    AppConfig,
    ConfigSource,
    ExecutionConfig,
    ProviderRetryConfig,
    RunRequest,
    Workspace,
)
from opencode_tools.errors import ConfigError

CONVENTIONAL_CONFIG_FILENAME = "opencode-tools.toml"
SUPPORTED_CONFIG_VERSION = 1

_TOP_LEVEL_KEYS = frozenset(
    {"version", "execution", "provider_retry", "runtime", "github"}
)
_EXECUTION_KEYS = frozenset(
    {
        "opencode_timeout_seconds",
        "utility_timeout_seconds",
        "termination_grace_seconds",
        "max_review_cycles",
    }
)
_PROVIDER_RETRY_KEYS = frozenset(
    {"max_attempts", "initial_delay_seconds", "multiplier", "max_delay_seconds"}
)
_RUNTIME_KEYS = frozenset({"root"})
_GITHUB_KEYS = frozenset({"targets"})
_GITHUB_TARGET_ENTRY_KEYS = frozenset({"remote", "repository"})

_DEFAULT_OPENCODE_TIMEOUT_SECONDS = 1800
_DEFAULT_UTILITY_TIMEOUT_SECONDS = 30
_DEFAULT_TERMINATION_GRACE_SECONDS = 5
_DEFAULT_MAX_REVIEW_CYCLES = 3
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_INITIAL_DELAY_SECONDS = 2
_DEFAULT_MULTIPLIER = 2.0
_DEFAULT_MAX_DELAY_SECONDS = 30
_DEFAULT_RUNTIME_ROOT = ".opencode-tools"

_CONFIG_SOURCE_BY_RAW = {
    "explicit": ConfigSource.EXPLICIT,
    "conventional": ConfigSource.CONVENTIONAL,
    "defaults": ConfigSource.DEFAULTS,
}


def resolve_workspace(workspace: Path, *, cwd: Path) -> Workspace:
    """Resolve `workspace` to an existing, canonical, absolute directory.

    A relative `workspace` is resolved against `cwd`; symlinks are resolved
    so the returned `Workspace.root` is stable and canonical.
    """

    candidate = workspace if workspace.is_absolute() else cwd / workspace
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise ConfigError(
            "config.workspace_not_found",
            f"workspace does not exist: {candidate}",
        ) from None
    if not resolved.is_dir():
        raise ConfigError(
            "config.workspace_not_directory",
            f"workspace is not a directory: {resolved}",
        )
    return Workspace(root=resolved)


def resolve_target_root(target: Path, *, workspace: Workspace) -> Path:
    """Resolve `target` to a canonical, existing path contained in `workspace`.

    `target` must be `.` or a relative path with no `..` component; after
    symlink resolution the result must still be contained in `workspace`.
    """

    if target.is_absolute():
        raise ConfigError(
            "config.target_not_relative",
            f"target must be '.' or a relative path: {target}",
        )
    if ".." in target.parts:
        raise ConfigError(
            "config.target_traversal",
            f"target must not contain '..': {target}",
        )
    candidate = workspace.root / target
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise ConfigError(
            "config.target_not_found",
            f"target does not exist: {candidate}",
        ) from None
    if not resolved.is_relative_to(workspace.root):
        raise ConfigError(
            "config.target_escapes_workspace",
            f"target escapes workspace after symlink resolution: {resolved}",
        )
    return resolved


@dataclass(frozen=True, slots=True)
class RawConfig:
    """Schema-shape-validated TOML data behind the effective configuration.

    Field-level type/range validation and the typed `AppConfig` it produces
    belong to a later milestone; this only fixes `source`, `path`, and the
    closed set of recognized v1 keys.
    """

    source: str
    path: Path | None
    data: Mapping[str, object]


def resolve_config_source(
    *,
    config_path: Path | None,
    workspace: Workspace,
    cwd: Path,
) -> tuple[str, Path | None]:
    """Resolve which config file, if any, is effective and its `source`.

    An explicit `config_path` is resolved against `cwd` and always wins.
    Otherwise the conventional `<workspace>/opencode-tools.toml` is used if
    present; a missing conventional file falls back to built-in defaults.
    """

    if config_path is not None:
        candidate = config_path if config_path.is_absolute() else cwd / config_path
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise ConfigError(
                "config.file_not_found",
                f"--config file does not exist: {candidate}",
            ) from None
        if not resolved.is_file():
            raise ConfigError(
                "config.file_not_a_file",
                f"--config path is not a file: {resolved}",
            )
        return "explicit", resolved

    conventional = workspace.root / CONVENTIONAL_CONFIG_FILENAME
    if conventional.is_file():
        return "conventional", conventional.resolve(strict=True)
    return "defaults", None


def _require_table(value: object, *, section: str, path: Path) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(
            "config.section_not_a_table",
            f"{section} must be a table in {path}",
        )
    return cast(Mapping[str, object], value)


def _reject_unknown_keys(
    data: Mapping[str, object],
    allowed: frozenset[str],
    *,
    section: str,
    path: Path,
) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(
            "config.unknown_key",
            f"{section} has unrecognized key(s) {unknown} in {path}",
        )


def _validate_schema_shape(data: Mapping[str, object], *, path: Path) -> None:
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, section="top-level", path=path)

    execution = _require_table(
        data.get("execution", {}), section="execution", path=path
    )
    _reject_unknown_keys(execution, _EXECUTION_KEYS, section="execution", path=path)

    provider_retry = _require_table(
        data.get("provider_retry", {}), section="provider_retry", path=path
    )
    _reject_unknown_keys(
        provider_retry, _PROVIDER_RETRY_KEYS, section="provider_retry", path=path
    )

    runtime = _require_table(data.get("runtime", {}), section="runtime", path=path)
    _reject_unknown_keys(runtime, _RUNTIME_KEYS, section="runtime", path=path)

    github = _require_table(data.get("github", {}), section="github", path=path)
    _reject_unknown_keys(github, _GITHUB_KEYS, section="github", path=path)

    targets = _require_table(
        github.get("targets", {}), section="github.targets", path=path
    )
    for target_key, entry in targets.items():
        entry_section = f"github.targets.{target_key}"
        entry_table = _require_table(entry, section=entry_section, path=path)
        _reject_unknown_keys(
            entry_table,
            _GITHUB_TARGET_ENTRY_KEYS,
            section=entry_section,
            path=path,
        )


def _check_version(data: Mapping[str, object], *, path: Path) -> None:
    if "version" not in data:
        raise ConfigError(
            "config.version_missing",
            f"config file is missing 'version': {path}",
        )
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError(
            "config.version_invalid_type",
            f"config 'version' must be an integer: {path}",
        )
    if version != SUPPORTED_CONFIG_VERSION:
        raise ConfigError(
            "config.version_unsupported",
            f"unsupported config version {version}: {path}",
        )


def read_config_file(path: Path) -> Mapping[str, object]:
    """Parse, version-check, and schema-shape-check one TOML config file."""

    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except OSError:
        raise ConfigError(
            "config.file_unreadable",
            f"config file could not be read: {path}",
        ) from None
    except tomllib.TOMLDecodeError:
        raise ConfigError(
            "config.file_invalid_toml",
            f"config file is not valid TOML: {path}",
        ) from None

    _check_version(data, path=path)
    _validate_schema_shape(data, path=path)
    return data


def load_raw_config(
    *,
    config_path: Path | None,
    workspace: Workspace,
    cwd: Path,
) -> RawConfig:
    """Discover, parse, and schema-shape-validate the effective config file."""

    source, resolved_path = resolve_config_source(
        config_path=config_path,
        workspace=workspace,
        cwd=cwd,
    )
    if resolved_path is None:
        return RawConfig(source=source, path=None, data={})
    data = read_config_file(resolved_path)
    return RawConfig(source=source, path=resolved_path, data=data)


def build_app_config(raw: RawConfig) -> AppConfig:
    """Apply v1 defaults, ranges, and cross-field rules to produce `AppConfig`.

    `raw.data` is already schema-shape-validated by `load_raw_config`; this
    only fills in per-key defaults and enforces the scalar/cross-field rules
    of `execution.*`, `provider_retry.*`, and `runtime.root`'s raw string.
    """

    execution_table = cast(Mapping[str, object], raw.data.get("execution", {}))
    provider_retry_table = cast(
        Mapping[str, object], raw.data.get("provider_retry", {})
    )
    runtime_table = cast(Mapping[str, object], raw.data.get("runtime", {}))

    try:
        execution = ExecutionConfig(
            opencode_timeout_seconds=cast(
                float,
                execution_table.get(
                    "opencode_timeout_seconds", _DEFAULT_OPENCODE_TIMEOUT_SECONDS
                ),
            ),
            utility_timeout_seconds=cast(
                float,
                execution_table.get(
                    "utility_timeout_seconds", _DEFAULT_UTILITY_TIMEOUT_SECONDS
                ),
            ),
            termination_grace_seconds=cast(
                float,
                execution_table.get(
                    "termination_grace_seconds", _DEFAULT_TERMINATION_GRACE_SECONDS
                ),
            ),
            max_review_cycles=cast(
                int,
                execution_table.get("max_review_cycles", _DEFAULT_MAX_REVIEW_CYCLES),
            ),
        )
        provider_retry = ProviderRetryConfig(
            max_attempts=cast(
                int, provider_retry_table.get("max_attempts", _DEFAULT_MAX_ATTEMPTS)
            ),
            initial_delay_seconds=cast(
                float,
                provider_retry_table.get(
                    "initial_delay_seconds", _DEFAULT_INITIAL_DELAY_SECONDS
                ),
            ),
            multiplier=cast(
                float, provider_retry_table.get("multiplier", _DEFAULT_MULTIPLIER)
            ),
            max_delay_seconds=cast(
                float,
                provider_retry_table.get(
                    "max_delay_seconds", _DEFAULT_MAX_DELAY_SECONDS
                ),
            ),
        )
        runtime_root = cast(str, runtime_table.get("root", _DEFAULT_RUNTIME_ROOT))
        return AppConfig(
            source=_CONFIG_SOURCE_BY_RAW[raw.source],
            execution=execution,
            provider_retry=provider_retry,
            runtime_root=runtime_root,
        )
    except TypeError as error:
        raise ConfigError("config.field_invalid_type", str(error)) from None
    except ValueError as error:
        raise ConfigError("config.field_invalid_value", str(error)) from None


def load_app_config(
    *,
    config_path: Path | None,
    workspace: Workspace,
    cwd: Path,
) -> AppConfig:
    """Discover, parse, and fully validate the effective `AppConfig`."""

    raw = load_raw_config(config_path=config_path, workspace=workspace, cwd=cwd)
    return build_app_config(raw)


def build_run_request(
    *,
    issue_number: int,
    workspace: Path,
    target: Path,
    cwd: Path,
) -> RunRequest:
    """Validate and assemble the `RunRequest` proven before any agent I/O."""

    resolved_workspace = resolve_workspace(workspace, cwd=cwd)
    resolved_target = resolve_target_root(target, workspace=resolved_workspace)
    try:
        return RunRequest(
            issue_number=issue_number,
            workspace=resolved_workspace,
            target_root=resolved_target,
        )
    except (TypeError, ValueError):
        raise ConfigError(
            "config.issue_number_invalid",
            "issue number must be a positive integer",
        ) from None


__all__ = (
    "CONVENTIONAL_CONFIG_FILENAME",
    "SUPPORTED_CONFIG_VERSION",
    "RawConfig",
    "build_app_config",
    "build_run_request",
    "load_app_config",
    "load_raw_config",
    "read_config_file",
    "resolve_config_source",
    "resolve_target_root",
    "resolve_workspace",
)

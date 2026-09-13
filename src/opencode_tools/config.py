"""Path resolution for the CLI-facing run request, proven before any I/O.

Only the workspace/target/issue-number shape of a `RunRequest` is resolved
here. The Git top-level proof, clean-baseline checks, runtime ownership/
ignore probes, and artifact creation remain the responsibility of later
milestones.
"""

from __future__ import annotations

from pathlib import Path

from opencode_tools.domain import RunRequest, Workspace
from opencode_tools.errors import ConfigError


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
    "build_run_request",
    "resolve_target_root",
    "resolve_workspace",
)

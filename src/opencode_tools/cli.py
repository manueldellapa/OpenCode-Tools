"""Argparse command shape for the single-issue `run` command.

Owns argv parsing and the process entrypoint only: workspace/target/issue
shape, a single positive issue number, and a relative target are proven
here, before any config file, adapter, or pipeline is touched. Loading
`AppConfig`, constructing adapters, and invoking the orchestrator belong to
the composition root landing in a later milestone; until then a
syntactically valid parse returns without promising `run.json` or a
`FINAL_STATUS` marker, and this module never imports `config.py` or
`orchestrator.py`.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

_PROG = "opencode-tools"


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"issue must be a positive integer: {raw!r}"
        ) from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"issue must be a positive integer: {raw!r}")
    return value


def _relative_target(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        raise argparse.ArgumentTypeError(
            f"target must be '.' or a relative path: {raw!r}"
        )
    return path


def build_parser() -> argparse.ArgumentParser:
    """Build the v0.1 command shape: a single `run` subcommand.

    `run` takes exactly `--workspace`, `--target`, `--issue`, and the
    optional `--config`; there are no lifecycle overrides, no batch/range
    issue forms, and no other subcommands in v0.1.
    """

    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Run the OpenCode Tools single-issue pipeline for one GitHub issue.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run the single-issue pipeline for one GitHub issue."
    )
    run_parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="OpenCode workspace root.",
    )
    run_parser.add_argument(
        "--target",
        required=True,
        type=_relative_target,
        help="Git target, relative to the workspace ('.' for the workspace root).",
    )
    run_parser.add_argument(
        "--issue",
        required=True,
        type=_positive_int,
        help="GitHub issue number (a positive integer).",
    )
    run_parser.add_argument(
        "--config",
        required=False,
        type=Path,
        default=None,
        help="Path to an explicit opencode-tools.toml config file.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse `argv` (default: `sys.argv[1:]`) against the v0.1 command shape."""

    return build_parser().parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: validate argv shape and return the process exit code.

    An invalid `argv` is rejected by `argparse` itself with exit code 2,
    before anything else runs. A syntactically valid parse returns 0;
    composing adapters and invoking the pipeline is out of scope until the
    composition root lands.
    """

    parse_args(argv)
    return 0


__all__ = ("build_parser", "main", "parse_args")

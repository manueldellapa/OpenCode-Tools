"""Unit tests for the v0.1 CLI command shape: parser, entrypoint, and help."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final, cast

import pytest

from opencode_tools.cli import build_parser, main, parse_args

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SOURCE_ROOT: Final = PROJECT_ROOT / "src"
MAIN_MODULE_PATH: Final = SOURCE_ROOT / "opencode_tools" / "__main__.py"

_VALID_ARGS: Final = (
    "run",
    "--workspace",
    "/tmp/workspace",
    "--target",
    ".",
    "--issue",
    "53",
)


# --- positive parsing ---------------------------------------------------


def test_parses_the_canonical_single_issue_shape() -> None:
    args = parse_args(_VALID_ARGS)

    assert args.command == "run"
    assert args.workspace == Path("/tmp/workspace")
    assert args.target == Path(".")
    assert args.issue == 53
    assert args.config is None


def test_target_accepts_a_nested_relative_path() -> None:
    args = parse_args((*_VALID_ARGS[:4], "backend/service", *_VALID_ARGS[5:]))

    assert args.target == Path("backend/service")


def test_config_is_optional_and_kept_as_a_plain_path() -> None:
    args = parse_args((*_VALID_ARGS, "--config", "custom.toml"))

    assert args.config == Path("custom.toml")


def test_main_returns_zero_and_prints_nothing_on_a_valid_parse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(_VALID_ARGS)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


# --- negative parsing: rejected before any run ---------------------------


@pytest.mark.parametrize("issue_value", ["0", "-1", "1.5", "abc", "1,2,3", "1-5", ""])
def test_rejects_non_positive_or_non_scalar_issue_values(issue_value: str) -> None:
    argv = (*_VALID_ARGS[:6], issue_value)

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


def test_rejects_an_absolute_target() -> None:
    argv = (*_VALID_ARGS[:4], "/etc/passwd", *_VALID_ARGS[5:])

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "missing_flags",
    [
        ("--workspace", "/tmp/workspace", "--target", "."),
        ("--workspace", "/tmp/workspace", "--issue", "53"),
        ("--target", ".", "--issue", "53"),
        (),
    ],
)
def test_rejects_missing_required_arguments(missing_flags: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(("run", *missing_flags))

    assert exc_info.value.code == 2


def test_rejects_an_unknown_option() -> None:
    argv = (*_VALID_ARGS, "--model", "gpt-5")

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


def test_rejects_an_unknown_subcommand() -> None:
    argv = ("validate", "--workspace", "/tmp/workspace")

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


def test_rejects_a_batch_issue_form() -> None:
    argv = (*_VALID_ARGS, "54")

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


def test_rejects_no_arguments_at_all() -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(())

    assert exc_info.value.code == 2


def test_a_syntax_error_prints_nothing_promising_an_artifact_or_marker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main((*_VALID_ARGS[:6], "-1"))

    captured = capsys.readouterr()
    assert "FINAL_STATUS" not in captured.out
    assert "FINAL_STATUS" not in captured.err
    assert "run.json" not in captured.out
    assert "run.json" not in captured.err


def test_a_syntax_error_never_reaches_main_return_value() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(("run", "--workspace", "/tmp/workspace"))

    assert exc_info.value.code == 2
    assert exc_info.value.code != 0


# --- help: documents only the v0.1 shape ----------------------------------


def test_top_level_help_lists_only_the_run_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(("--help",))

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "run" in out
    for future_subcommand in ("validate", "dry-run", "inspect"):
        assert future_subcommand not in out


def test_run_help_documents_exactly_the_four_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(("run", "--help"))

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for option in ("--workspace", "--target", "--issue", "--config"):
        assert option in out
    for forbidden in ("--format", "--json", "--interactive"):
        assert forbidden not in out


# --- __main__.py: delegates, no parsing or business logic of its own -----


def test_main_module_contains_no_parsing_or_business_logic() -> None:
    source = MAIN_MODULE_PATH.read_text(encoding="utf-8")

    assert "from opencode_tools.cli import main" in source
    assert "argparse" not in source
    assert "add_argument" not in source


def test_python_dash_m_invocation_exposes_the_same_run_interface() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SOURCE_ROOT)

    completed = subprocess.run(
        [sys.executable, "-m", "opencode_tools", "run", "--help"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    for option in ("--workspace", "--target", "--issue", "--config"):
        assert option in completed.stdout


def test_python_dash_m_invocation_rejects_a_syntax_error_with_exit_two() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SOURCE_ROOT)

    completed = subprocess.run(
        [sys.executable, "-m", "opencode_tools", "run", "--issue", "0"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "FINAL_STATUS" not in completed.stdout
    assert "FINAL_STATUS" not in completed.stderr


# --- pyproject.toml: canonical entry point --------------------------------


def test_pyproject_declares_the_canonical_console_script() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as manifest_file:
        manifest = cast(dict[str, object], tomllib.load(manifest_file))

    project = cast(dict[str, object], manifest["project"])
    scripts = cast(dict[str, object], project["scripts"])

    assert scripts == {"opencode-tools": "opencode_tools.cli:main"}


def test_build_parser_prog_matches_the_canonical_command_name() -> None:
    assert build_parser().prog == "opencode-tools"

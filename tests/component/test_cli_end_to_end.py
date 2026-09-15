"""Component tests for the CLI composition root end to end (M14-02/M14-03).

Drives `cli.main` through a real `git` repository, `gh` and `opencode`
faked via `tests/component/helpers/fake_gh.py`/`fake_opencode.py` shimmed
onto `PATH` (so `main`'s own unconditional `shutil.which`-based executable
resolution finds them exactly as it would a real install), a real runtime
root on disk, and an explicit `--config` overriding the GitHub repository
identity so no real network or `git remote` is ever needed. No fixture or
mock of `ProcessRunner`/`GitSafetyPort`/etc. is used anywhere here -- that
is `tests/unit/test_cli.py`'s job; this proves the *real* adapters wire
together correctly: workspace as OpenCode's cwd, target explicit for Git,
no forbidden flags, a pre-init failure (a dirty target) never creating a
run directory or `run.json`, and -- since M14-03 -- that `main`'s own
rendering (the `FINAL_STATUS` line, exit code, and stderr summary) is
consistent with the real `IssueResult`/`run.json` a full pipeline run
produces, on both the happy and the failure path.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from opencode_tools.cli import main

HELPERS = Path(__file__).resolve().parent / "helpers"
FAKE_GH = HELPERS / "fake_gh.py"
FAKE_OPENCODE = HELPERS / "fake_opencode.py"
_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "opencode" / "1.17.18"
)
DEBUG_FIXTURES = _FIXTURE_ROOT / "debug"
RUN_FIXTURES = _FIXTURE_ROOT / "run"
EXPORT_FIXTURES = _FIXTURE_ROOT / "export"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True
    )


def _clean_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)


def _shim_path(bin_dir: Path) -> str:
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "gh").symlink_to(FAKE_GH)
    (bin_dir / "opencode").symlink_to(FAKE_OPENCODE)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def _config_file(
    path: Path, *, runtime_root: Path, target_workspace_relative: str = "."
) -> None:
    path.write_text(
        "version = 1\n"
        "\n"
        "[execution]\n"
        "opencode_timeout_seconds = 30\n"
        "utility_timeout_seconds = 10\n"
        "termination_grace_seconds = 2\n"
        "max_review_cycles = 3\n"
        "\n"
        "[runtime]\n"
        f'root = "{runtime_root.as_posix()}"\n'
        "\n"
        f'[github.targets."{target_workspace_relative}"]\n'
        'repository = "octocat/hello-world"\n',
        encoding="utf-8",
    )


def _set_opencode_debug_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_CONFIG_FILE", str(DEBUG_FIXTURES / "config-baseline.json")
    )
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_ARCHITECT_FILE",
        str(DEBUG_FIXTURES / "agent-architect-baseline.json"),
    )
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_CODER_FILE",
        str(DEBUG_FIXTURES / "agent-coder-baseline.json"),
    )
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_REVIEWER_FILE",
        str(DEBUG_FIXTURES / "agent-reviewer-baseline.json"),
    )


def _write_export_fixture(path: Path, *, session_id: str, agent: str) -> None:
    path.write_text(
        json.dumps(
            {
                "sessionID": session_id,
                "messages": [
                    {
                        "info": {"id": "msg_1", "role": "assistant", "agent": agent},
                        "parts": [{"type": "text", "text": "AGENT_STATUS: FAILED"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_architect_reported_failure_writes_a_failed_run_via_real_git_and_faked_gh_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    _clean_repo(workspace)
    runtime_root = tmp_path / "runtime"
    config_path = tmp_path / "opencode-tools.toml"
    _config_file(config_path, runtime_root=runtime_root)

    monkeypatch.setenv("PATH", _shim_path(tmp_path / "bin"))
    _set_opencode_debug_fixtures(monkeypatch)
    monkeypatch.setenv(
        "FAKE_OPENCODE_RUN_OUTPUT_FILE", str(RUN_FIXTURES / "architect-failed.ndjson")
    )
    export_file = tmp_path / "export-architect-failed.json"
    _write_export_fixture(
        export_file, session_id="ses_architect_failed", agent="architect"
    )
    monkeypatch.setenv("FAKE_OPENCODE_EXPORT_FILE", str(export_file))
    call_log = tmp_path / "opencode-calls.log"
    monkeypatch.setenv("FAKE_OPENCODE_CALL_LOG_FILE", str(call_log))

    exit_code = main(
        [
            "run",
            "--workspace",
            str(workspace),
            "--target",
            ".",
            "--issue",
            "42",
            "--config",
            str(config_path),
        ]
    )

    # AGENT_REPORTED_FAILURE is in the 20 family (System Design SS13.4);
    # `main`'s own rendering (M14-03) must agree with the persisted record.
    assert exit_code == 20

    run_json_paths = list(runtime_root.rglob("run.json"))
    assert len(run_json_paths) == 1
    record = json.loads(run_json_paths[0].read_text(encoding="utf-8"))
    assert record["final_status"] == "FAILED"
    assert record["trigger_outcome"] == "AGENT_REPORTED_FAILURE"
    assert record["current_phase"] == "FINISHED"
    assert record["git_safety_status"] == "SAFE"
    assert record["persistence_status"] == "OK"
    assert record["issue_number"] == 42
    assert Path(record["workspace"]["root"]) == workspace.resolve()
    assert Path(record["target"]["root"]) == workspace.resolve()
    assert record["issue_locator"]["repository_identity"]["repository"] == "hello-world"
    assert record["issue_locator"]["repository_identity"]["source"] == "override"
    # Exactly one logical invocation (the architect) ran -- never a coder.
    assert len(record["attempts"]) == 1
    assert record["attempts"][0]["role"] == "ARCHITECT"

    # `opencode` only ever saw `run --agent architect ...` and the matching
    # `export ... --sanitize`; never `--auto`, `--share`, or `--model`.
    calls = call_log.read_text(encoding="utf-8").splitlines()
    run_calls = [line for line in calls if line.startswith("run --agent")]
    assert len(run_calls) == 1
    assert "--agent architect" in run_calls[0]
    assert f"--dir {workspace.resolve()}" in run_calls[0]
    for forbidden in ("--auto", "--share", "--model"):
        for call in calls:
            assert forbidden not in call

    # M14-03's own rendering contract: exactly one `FINAL_STATUS` line on
    # stdout, agreeing with `run.json`'s `final_status`, and nothing else
    # there; a display-safe summary -- consistent with the same record --
    # on stderr, never containing the `FINAL_STATUS` token itself.
    captured = capsys.readouterr()
    assert captured.out == "FINAL_STATUS: FAILED\n"
    assert "FINAL_STATUS" not in captured.err
    assert f"run_id: {record['run_id']}" in captured.err
    assert "phase: FINISHED" in captured.err
    assert "terminal outcome: AGENT_REPORTED_FAILURE" in captured.err
    assert str(run_json_paths[0]) in captured.err
    assert "changes preserved: yes" in captured.err
    assert "staged=0 unstaged=0 untracked=0" in captured.err


def test_a_dirty_target_fails_before_any_run_directory_or_artifact_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dirty working tree is rejected by `resolve_target`, bootstrap's own
    very first step -- strictly before any lease, run directory, or
    `run.json` can ever exist (M14-02's own AC) -- so `main` (M14-03) must
    reject it with the PREFLIGHT_ERROR family exit code and print no
    `FINAL_STATUS` line or artifact promise anywhere."""

    workspace = tmp_path / "workspace"
    _clean_repo(workspace)
    (workspace / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    runtime_root = tmp_path / "runtime"
    config_path = tmp_path / "opencode-tools.toml"
    _config_file(config_path, runtime_root=runtime_root)

    monkeypatch.setenv("PATH", _shim_path(tmp_path / "bin"))
    _set_opencode_debug_fixtures(monkeypatch)

    exit_code = main(
        [
            "run",
            "--workspace",
            str(workspace),
            "--target",
            ".",
            "--issue",
            "42",
            "--config",
            str(config_path),
        ]
    )

    # PREFLIGHT_ERROR is in the 10 family (System Design SS13.4).
    assert exit_code == 10
    assert list(runtime_root.rglob("run.json")) == []

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "FINAL_STATUS" not in captured.err
    assert "artifact: none" in captured.err
    assert "terminal outcome: PREFLIGHT_ERROR" in captured.err
    assert "git_safety.dirty_worktree" in captured.err


def test_a_full_approved_pipeline_renders_exit_zero_and_one_final_status_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one scenario where every gate (System Design SS8.4) is met: the
    architect, coder, and reviewer each succeed in turn -- via one `run`/
    `export` fixture pair served per role, from `fake_opencode.py`'s
    sequential-file mode -- and the target's branch/HEAD never move, so
    `finalize_run` reaches `FinalStatus.APPROVED`. `main` must render
    `FINAL_STATUS: APPROVED` (and only that) on stdout and exit 0."""

    workspace = tmp_path / "workspace"
    _clean_repo(workspace)
    runtime_root = tmp_path / "runtime"
    config_path = tmp_path / "opencode-tools.toml"
    _config_file(config_path, runtime_root=runtime_root)

    monkeypatch.setenv("PATH", _shim_path(tmp_path / "bin"))
    _set_opencode_debug_fixtures(monkeypatch)
    monkeypatch.setenv(
        "FAKE_OPENCODE_RUN_OUTPUT_FILES",
        os.pathsep.join(
            str(RUN_FIXTURES / name)
            for name in (
                "architect-ready-success.ndjson",
                "coder-completed-success.ndjson",
                "reviewer-approved-success.ndjson",
            )
        ),
    )
    monkeypatch.setenv(
        "FAKE_OPENCODE_RUN_OUTPUT_INDEX_FILE", str(tmp_path / "run-output-index")
    )
    monkeypatch.setenv(
        "FAKE_OPENCODE_EXPORT_FILES",
        os.pathsep.join(
            str(EXPORT_FIXTURES / name)
            for name in (
                "architect-correct-agent.json",
                "coder-correct-agent.json",
                "reviewer-correct-agent.json",
            )
        ),
    )
    monkeypatch.setenv(
        "FAKE_OPENCODE_EXPORT_INDEX_FILE", str(tmp_path / "export-index")
    )
    call_log = tmp_path / "opencode-calls.log"
    monkeypatch.setenv("FAKE_OPENCODE_CALL_LOG_FILE", str(call_log))

    exit_code = main(
        [
            "run",
            "--workspace",
            str(workspace),
            "--target",
            ".",
            "--issue",
            "42",
            "--config",
            str(config_path),
        ]
    )

    run_json_paths = list(runtime_root.rglob("run.json"))
    assert len(run_json_paths) == 1
    record = json.loads(run_json_paths[0].read_text(encoding="utf-8"))
    assert record["final_status"] == "APPROVED"
    assert record["trigger_outcome"] == "SUCCEEDED"
    assert [attempt["role"] for attempt in record["attempts"]] == [
        "ARCHITECT",
        "CODER",
        "REVIEWER",
    ]

    assert exit_code == 0

    calls = call_log.read_text(encoding="utf-8").splitlines()
    run_calls = [line for line in calls if line.startswith("run --agent")]
    assert [call.split()[2] for call in run_calls] == ["architect", "coder", "reviewer"]

    # Exactly one `FINAL_STATUS` line, on stdout only; the stderr summary
    # is consistent with the same persisted record and never repeats it.
    captured = capsys.readouterr()
    assert captured.out == "FINAL_STATUS: APPROVED\n"
    assert "FINAL_STATUS" not in captured.err
    assert f"run_id: {record['run_id']}" in captured.err
    assert "phase: FINISHED" in captured.err
    assert "terminal outcome: SUCCEEDED" in captured.err
    assert str(run_json_paths[0]) in captured.err
    assert "changes preserved: yes" in captured.err
    assert "staged=0 unstaged=0 untracked=0" in captured.err

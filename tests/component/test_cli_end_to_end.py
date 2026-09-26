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

import base64
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

    # Issue #117 regression: the production `_CliAgentRunner` must wrap the
    # real process execution with the runner-channel records that
    # `AttemptLogFileSink` already supports.
    process = record["attempts"][0]["agent_result"]["process"]
    attempt_log_path = run_json_paths[0].parent / process["log_path"]
    attempt_log_records = [
        json.loads(line)
        for line in attempt_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert attempt_log_records[0]["channel"] == "runner"
    assert attempt_log_records[-1]["channel"] == "runner"

    runner_events = [
        json.loads(base64.b64decode(log_record["payload_base64"]))
        for log_record in attempt_log_records
        if log_record["channel"] == "runner"
    ]
    assert runner_events == [
        {
            "event": "header",
            "command": [*process["command"], "<PROMPT_REDACTED>"],
            "cwd": process["cwd"],
        },
        {
            "event": "footer",
            "outcome": process["outcome"],
            "duration_ns": process["duration_ns"],
        },
    ]

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


def test_multi_repo_pipeline_runs_coder_in_disposable_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for #88/#90: architect/reviewer keep workspace context,
    while the coder runs in an isolated clone and retains the workspace-owned
    OpenCode control plane."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".opencode").mkdir()
    target = workspace / "Backend"
    _clean_repo(target)

    runtime_root = tmp_path / "runtime"
    config_path = tmp_path / "opencode-tools.toml"
    _config_file(
        config_path,
        runtime_root=runtime_root,
        target_workspace_relative="Backend",
    )

    monkeypatch.setenv("PATH", _shim_path(tmp_path / "bin"))
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
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
    context_log = tmp_path / "opencode-contexts.jsonl"
    monkeypatch.setenv("FAKE_OPENCODE_CONTEXT_LOG_FILE", str(context_log))

    exit_code = main(
        [
            "run",
            "--workspace",
            str(workspace),
            "--target",
            "Backend",
            "--issue",
            "42",
            "--config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    run_json_paths = list(runtime_root.rglob("run.json"))
    assert len(run_json_paths) == 1
    record = json.loads(run_json_paths[0].read_text(encoding="utf-8"))
    assert record["final_status"] == "APPROVED"
    assert Path(record["workspace"]["root"]) == workspace.resolve()
    assert Path(record["target"]["root"]) == target.resolve()

    contexts = [
        json.loads(line)
        for line in context_log.read_text(encoding="utf-8").splitlines()
    ]
    run_contexts = [item for item in contexts if item["argv"][:2] == ["run", "--agent"]]
    assert [item["argv"][2] for item in run_contexts] == [
        "architect",
        "coder",
        "reviewer",
    ]
    assert run_contexts[0]["cwd"] == str(workspace.resolve())
    assert run_contexts[0]["config_dir"] is None
    coder_cwd = Path(run_contexts[1]["cwd"])
    assert coder_cwd != target.resolve()
    assert coder_cwd.name == "target"
    assert coder_cwd.parent.name.startswith("opencode-tools-coder-sandbox-")
    assert run_contexts[1]["config_dir"] == str(workspace.resolve() / ".opencode")
    assert run_contexts[2]["cwd"] == str(workspace.resolve())
    assert run_contexts[2]["config_dir"] is None

    assert (
        Path(record["attempts"][0]["agent_result"]["process"]["cwd"])
        == workspace.resolve()
    )
    assert Path(record["attempts"][1]["agent_result"]["process"]["cwd"]) == coder_cwd
    assert (
        Path(record["attempts"][2]["agent_result"]["process"]["cwd"])
        == workspace.resolve()
    )

    captured = capsys.readouterr()
    assert captured.out == "FINAL_STATUS: APPROVED\n"


# --- AC-001: single positive issue accepted; batch/non-positive rejected ---


def test_ac_001_single_positive_issue_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD AC-001: "il comando canonico avvia una issue valida e rifiuta
    batch o issue non positive." The accept half is proven here through the
    real `main()` composition -- not merely `parse_args` -- reusing the
    same single-role architect-failure fixture
    `test_architect_reported_failure_writes_a_failed_run_via_real_git_and_
    faked_gh_opencode` drives: reaching real execution (a `run.json` on
    disk, a non-argparse exit code) is what distinguishes "accepted" from
    an argparse-level rejection. The reject half needs no git/gh/opencode
    setup at all -- `argparse` rejects a non-positive or batch `--issue`
    before `main` ever touches an adapter, exactly as `tests/unit/
    test_cli.py::test_rejects_non_positive_or_non_scalar_issue_values`/
    `::test_rejects_a_batch_issue_form` already prove at the parser level;
    this corroborates the same rejected values through the real end-to-end
    entry point instead of `parse_args` alone.
    """

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

    base_args = [
        "run",
        "--workspace",
        str(workspace),
        "--target",
        ".",
        "--config",
        str(config_path),
    ]

    # Accept: a single positive issue proceeds past argument parsing into
    # real execution -- proven by a real `run.json` on disk, never merely a
    # nonzero exit code (which a rejection also produces).
    exit_code = main([*base_args, "--issue", "42"])
    assert exit_code == 20
    assert len(list(runtime_root.rglob("run.json"))) == 1

    # Reject: a non-positive issue is rejected by argparse itself, before
    # any adapter is ever touched -- the same `SystemExit(2)` `tests/unit/
    # test_cli.py::test_rejects_non_positive_or_non_scalar_issue_values`
    # already proves at the parser level for this same value.
    with pytest.raises(SystemExit) as non_positive_error:
        main([*base_args, "--issue", "0"])
    assert non_positive_error.value.code == 2

    # Reject: a batch/repeated-issue form is rejected the same way, the
    # same shape `tests/unit/test_cli.py::test_rejects_a_batch_issue_form`
    # already proves at the parser level.
    with pytest.raises(SystemExit) as batch_error:
        main([*base_args, "--issue", "42", "54"])
    assert batch_error.value.code == 2


# --- AC-025: final status/exit code/run.json consistency, plus the two
# explicit pre-init and logging-failure exceptions the PRD names ----------


def test_ac_025_final_status_exit_and_run_json_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PRD AC-025: "ogni run inizializzato produce esattamente un final
    status coerente con exit code e, quando la persistenza finale riesce,
    con run.json; errori pre-inizializzazione e logging failure seguono le
    eccezioni esplicite del PRD." Four branches, each through the real
    `main()` composition:

    (1) FAILED/AGENT_REPORTED_FAILURE/exit-20 -- reusing
        `test_architect_reported_failure_writes_a_failed_run_via_real_git_
        and_faked_gh_opencode`'s single-fixture wiring;
    (2) APPROVED/exit-0 -- reusing
        `test_a_full_approved_pipeline_renders_exit_zero_and_one_final_
        status_line`'s three-fixture, sequential wiring (run *after* (1)
        so its `FAKE_OPENCODE_RUN_OUTPUT_FILES`/`EXPORT_FILES` -- which
        `fake_opencode.py` prefers over the singular `_FILE` variables
        whenever set -- are never left stale for a later branch that
        expects the singular form);
    (3) the pre-initialization exception -- PREFLIGHT_ERROR/exit-10, zero
        `run.json` anywhere -- reusing
        `test_a_dirty_target_fails_before_any_run_directory_or_artifact_
        exists`'s dirty-worktree setup;
    (4) the PRD's other explicit exception, a genuine *logging* failure --
        reached for real, never faked, by denying write access to the
        runtime root (`chmod 0o500`) before `main()` runs, so
        `runlog.create_run_directory`'s real `os.mkdir` fails with a
        permission `OSError` that `runlog._ensure_runs_root` wraps into a
        `LoggingError` (`runlog.directory_create_failed`). That happens
        inside `bootstrap_run`'s step 3 -- `RunStorePort.initialize` --
        strictly before any run directory or `run.json` can exist, so
        `main` renders it exactly like branch (3)'s pre-init shape (no
        `FINAL_STATUS` line, no artifact promise) but in the distinct
        LOGGING_ERROR/exit-40 family, which is the concrete case this AC's
        "logging failure" exception names. (This sandbox runs as a
        non-root user, confirmed by `os.geteuid() != 0` below, so the
        permission denial is real and not silently bypassed.) The other,
        already-provable shapes of a logging failure -- a first-persist
        failure, and a mid-subprocess sink fault -- stay covered by
        `tests/component/test_pipeline_failures.py::
        test_a_first_persist_failure_blocks_everything_before_the_
        baseline`, `tests/component/test_process_runner.py::
        test_run_terminates_the_child_and_reports_logging_error_on_a_
        sink_fault`, and `tests/unit/test_cli.py`'s own exit-code mapping
        table -- named here rather than re-derived.
    """

    # --- (1) FAILED / AGENT_REPORTED_FAILURE / exit 20 --------------------
    failure_workspace = tmp_path / "failure-workspace"
    _clean_repo(failure_workspace)
    failure_runtime_root = tmp_path / "failure-runtime"
    failure_config = tmp_path / "failure-opencode-tools.toml"
    _config_file(failure_config, runtime_root=failure_runtime_root)

    monkeypatch.setenv("PATH", _shim_path(tmp_path / "failure-bin"))
    _set_opencode_debug_fixtures(monkeypatch)
    monkeypatch.setenv(
        "FAKE_OPENCODE_RUN_OUTPUT_FILE", str(RUN_FIXTURES / "architect-failed.ndjson")
    )
    failure_export_file = tmp_path / "export-architect-failed.json"
    _write_export_fixture(
        failure_export_file, session_id="ses_architect_failed", agent="architect"
    )
    monkeypatch.setenv("FAKE_OPENCODE_EXPORT_FILE", str(failure_export_file))

    failure_exit_code = main(
        [
            "run",
            "--workspace",
            str(failure_workspace),
            "--target",
            ".",
            "--issue",
            "42",
            "--config",
            str(failure_config),
        ]
    )

    failure_run_json_paths = list(failure_runtime_root.rglob("run.json"))
    assert len(failure_run_json_paths) == 1
    failure_record = json.loads(failure_run_json_paths[0].read_text(encoding="utf-8"))
    assert failure_record["final_status"] == "FAILED"
    assert failure_record["trigger_outcome"] == "AGENT_REPORTED_FAILURE"
    assert failure_exit_code == 20

    failure_captured = capsys.readouterr()
    assert failure_captured.out == "FINAL_STATUS: FAILED\n"
    assert "FINAL_STATUS" not in failure_captured.err
    assert "terminal outcome: AGENT_REPORTED_FAILURE" in failure_captured.err
    assert str(failure_run_json_paths[0]) in failure_captured.err

    # --- (2) APPROVED / exit 0 --------------------------------------------
    approved_workspace = tmp_path / "approved-workspace"
    _clean_repo(approved_workspace)
    approved_runtime_root = tmp_path / "approved-runtime"
    approved_config = tmp_path / "approved-opencode-tools.toml"
    _config_file(approved_config, runtime_root=approved_runtime_root)

    monkeypatch.setenv("PATH", _shim_path(tmp_path / "approved-bin"))
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
        "FAKE_OPENCODE_RUN_OUTPUT_INDEX_FILE",
        str(tmp_path / "approved-run-output-index"),
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
        "FAKE_OPENCODE_EXPORT_INDEX_FILE", str(tmp_path / "approved-export-index")
    )

    approved_exit_code = main(
        [
            "run",
            "--workspace",
            str(approved_workspace),
            "--target",
            ".",
            "--issue",
            "42",
            "--config",
            str(approved_config),
        ]
    )

    approved_run_json_paths = list(approved_runtime_root.rglob("run.json"))
    assert len(approved_run_json_paths) == 1
    approved_record = json.loads(approved_run_json_paths[0].read_text(encoding="utf-8"))
    assert approved_record["final_status"] == "APPROVED"
    assert approved_record["trigger_outcome"] == "SUCCEEDED"
    assert approved_exit_code == 0

    approved_captured = capsys.readouterr()
    assert approved_captured.out == "FINAL_STATUS: APPROVED\n"
    assert "FINAL_STATUS" not in approved_captured.err
    assert "terminal outcome: SUCCEEDED" in approved_captured.err
    assert str(approved_run_json_paths[0]) in approved_captured.err

    # --- (3) pre-init exception: PREFLIGHT_ERROR / exit 10, no run.json --
    dirty_workspace = tmp_path / "dirty-workspace"
    _clean_repo(dirty_workspace)
    (dirty_workspace / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    dirty_runtime_root = tmp_path / "dirty-runtime"
    dirty_config = tmp_path / "dirty-opencode-tools.toml"
    _config_file(dirty_config, runtime_root=dirty_runtime_root)

    monkeypatch.setenv("PATH", _shim_path(tmp_path / "dirty-bin"))
    _set_opencode_debug_fixtures(monkeypatch)

    dirty_exit_code = main(
        [
            "run",
            "--workspace",
            str(dirty_workspace),
            "--target",
            ".",
            "--issue",
            "42",
            "--config",
            str(dirty_config),
        ]
    )

    assert dirty_exit_code == 10
    assert list(dirty_runtime_root.rglob("run.json")) == []

    dirty_captured = capsys.readouterr()
    assert dirty_captured.out == ""
    assert "FINAL_STATUS" not in dirty_captured.err
    assert "artifact: none" in dirty_captured.err
    assert "terminal outcome: PREFLIGHT_ERROR" in dirty_captured.err

    # --- (4) the PRD's other explicit exception: a genuine logging failure
    assert os.geteuid() != 0, (
        "permission-based fault injection needs a non-root process; running "
        "as root would silently bypass the denial this branch relies on"
    )

    logging_failure_workspace = tmp_path / "logging-failure-workspace"
    _clean_repo(logging_failure_workspace)
    logging_failure_runtime_root = tmp_path / "logging-failure-runtime"
    logging_failure_config = tmp_path / "logging-failure-opencode-tools.toml"
    _config_file(logging_failure_config, runtime_root=logging_failure_runtime_root)

    monkeypatch.setenv("PATH", _shim_path(tmp_path / "logging-failure-bin"))
    _set_opencode_debug_fixtures(monkeypatch)

    # `bootstrap_runtime_root` accepts a pre-existing root as long as it is
    # a real, non-symlink, owned directory whose mode does not exceed
    # `0700` (`runlog._verify_existing_runtime_root`) -- it never requires
    # the write bit -- so a root already denied write access still passes
    # that check; the write denial only bites one step later, inside
    # `RunStorePort.initialize` -> `runlog.create_run_directory` ->
    # `_ensure_runs_root`'s own `os.mkdir(runs_root, ...)`.
    logging_failure_runtime_root.mkdir()
    logging_failure_runtime_root.chmod(0o500)
    try:
        logging_failure_exit_code = main(
            [
                "run",
                "--workspace",
                str(logging_failure_workspace),
                "--target",
                ".",
                "--issue",
                "42",
                "--config",
                str(logging_failure_config),
            ]
        )
    finally:
        logging_failure_runtime_root.chmod(0o700)

    assert logging_failure_exit_code == 40
    assert list(logging_failure_runtime_root.rglob("run.json")) == []

    logging_failure_captured = capsys.readouterr()
    assert logging_failure_captured.out == ""
    assert "FINAL_STATUS" not in logging_failure_captured.err
    assert "artifact: none" in logging_failure_captured.err
    assert "terminal outcome: LOGGING_ERROR" in logging_failure_captured.err
    assert "runlog.directory_create_failed" in logging_failure_captured.err

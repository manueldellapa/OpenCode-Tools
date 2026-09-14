"""Unit tests for the `run.json` v1 serialization (M08-02).

`RunRecord`'s own invariants -- nullable dimensions, immutability, JSON-tree
validity -- are already exhaustively covered by `tests/unit/test_domain.py`;
these tests instead cover `runlog.serialize_run_record()`, the function that
turns a `RunRecord` into the exact on-disk `run.json` v1 bytes (System
Design SS15.3): a golden fixture pinning the byte-for-byte format, stability
across repeated calls, every canonical group surviving into the bytes,
multiple ordered causes, RFC 3339 timestamps and `duration_ns`, and that a
real sanitized `AppConfig` never leaks a credential or a full environment
dump through it. Atomic replace and failure semantics are M08-03 and are not
exercised here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from opencode_tools.config import sanitize_app_config
from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AppConfig,
    AttemptRecord,
    ConfigSource,
    ErrorRecord,
    ExecutionConfig,
    FinalStatus,
    FrozenJsonValue,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    IssueLocator,
    IssueRef,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    ProviderRetryConfig,
    RepositoryIdentity,
    RunOutcome,
    RunRecord,
    TargetRepository,
    Workspace,
    to_primitive,
)
from opencode_tools.runlog import persist_run_record, serialize_run_record

WORKSPACE_ROOT = Path("/workspaces/opencode-tools")
TARGET_ROOT = WORKSPACE_ROOT / "backend"
ARTIFACT_PATH = WORKSPACE_ROOT / ".opencode-tools/runs/run-001/run.json"
NOW = datetime(2026, 9, 11, 14, 23, 45, 123456, tzinfo=UTC)
LATER = datetime(2026, 9, 11, 14, 24, 1, 654321, tzinfo=UTC)


def _minimal_run_record() -> RunRecord:
    return RunRecord(
        schema_version=1,
        run_id="run-001",
        artifact_path=ARTIFACT_PATH,
        workspace=Workspace(root=WORKSPACE_ROOT),
        target=TargetRepository(
            root=TARGET_ROOT,
            workspace_relative=Path("backend"),
            git_common_dir=TARGET_ROOT / ".git",
        ),
        issue_number=4,
        config={},
        environment={},
        started_at=NOW,
        current_phase=PipelinePhase.PREFLIGHT,
        persistence_status=PersistenceStatus.OK,
    )


def _process_result(*, log_path: Path) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/git", "status"),
        cwd=TARGET_ROOT,
        started_at=NOW,
        finished_at=LATER,
        duration_ns=1_530_865_000,
        return_code=0,
        timed_out=False,
        termination_confirmed=True,
        log_path=log_path,
        stdout_byte_count=4,
        stdout_sha256="stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="stderr-digest",
        outcome=RunOutcome.SUCCEEDED,
    )


def _git_state() -> GitState:
    return GitState(
        root=TARGET_ROOT,
        branch="feat/m08-runtime-store",
        head="0123456789abcdef",
        porcelain_summary="clean",
        staged=(),
        unstaged=(),
        untracked=(),
        fingerprint="fingerprint-001",
    )


def _git_check(*, sequence: int, purpose: str) -> GitCheckRecord:
    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=(_process_result(log_path=Path(f"git-check-{sequence}.log")),),
        state=_git_state(),
        safety_status=GitSafetyStatus.SAFE,
    )


def _attempt_record() -> AttemptRecord:
    agent_result = AgentResult(
        role=AgentRole.CODER,
        phase=PipelinePhase.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_process_result(log_path=Path("coder-cycle-1-provider-attempt-1.log")),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.CODER,
            body="Implemented the requested change.",
            agent_status=None,
        ),
        session_id="session-001",
        verified_agent="coder",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )
    return AttemptRecord(
        logical_invocation_id="coder-cycle-1",
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        git_before=_git_check(sequence=1, purpose="attempt-before"),
        git_after=_git_check(sequence=2, purpose="attempt-after"),
        agent_result=agent_result,
        retry_decision=False,
    )


def _error_record(*, sequence: int, code: str) -> ErrorRecord:
    return ErrorRecord(
        sequence=sequence,
        timestamp=LATER,
        phase=PipelinePhase.POSTFLIGHT,
        outcome=RunOutcome.GIT_SAFETY_ERROR,
        code=code,
        message=f"failure {code}",
        related_record="git-check-2",
    )


def _full_run_record() -> RunRecord:
    identity = RepositoryIdentity(
        host="github.com",
        owner="example",
        repository="backend",
        source="origin",
        remote_name="origin",
    )
    locator = IssueLocator(repository_identity=identity, number=4)
    issue_ref = IssueRef(
        locator=locator,
        url="https://github.com/example/backend/issues/4",
        title="Implement run.json schema v1",
    )
    return RunRecord(
        schema_version=1,
        run_id="run-001",
        artifact_path=ARTIFACT_PATH,
        workspace=Workspace(root=WORKSPACE_ROOT),
        target=TargetRepository(
            root=TARGET_ROOT,
            workspace_relative=Path("backend"),
            git_common_dir=TARGET_ROOT / ".git",
        ),
        issue_number=4,
        config={"source": "defaults"},
        environment={
            "tool_version": "0.1.0",
            "python_version": "3.13.15",
            "platform": "Darwin",
            "opencode_version": "1.17.18",
            "git_version": "2.43.0",
            "gh_version": "2.40.0",
        },
        started_at=NOW,
        current_phase=PipelinePhase.FINISHED,
        persistence_status=PersistenceStatus.OK,
        issue_locator=locator,
        issue_ref=issue_ref,
        finished_at=LATER,
        duration_ns=15_865_000,
        review_cycle=1,
        provider_attempt=1,
        terminal_outcome=RunOutcome.SUCCEEDED,
        git_baseline=_git_state(),
        git_checks=(_git_check(sequence=0, purpose="baseline"),),
        git_postflight=_git_check(sequence=3, purpose="postflight"),
        git_safety_status=GitSafetyStatus.SAFE,
        timeline=({"sequence": 0, "phase": "PREFLIGHT", "event": "run_initialized"},),
        attempts=(_attempt_record(),),
        errors=(
            _error_record(sequence=4, code="git.branch_drift"),
            _error_record(sequence=5, code="git.uncommitted_change"),
        ),
        last_successful_sequence=3,
        artifact_incomplete=True,
        trigger_outcome=RunOutcome.GIT_SAFETY_ERROR,
        final_status=FinalStatus.FAILED,
        expected_exit_code=10,
        changes_preserved=True,
        termination_confirmed=True,
    )


def test_serialize_run_record_rejects_a_non_run_record() -> None:
    with pytest.raises(TypeError, match="record must be RunRecord"):
        serialize_run_record(cast(RunRecord, {"not": "a run record"}))


def test_persist_run_record_rejects_a_non_run_record() -> None:
    # Real atomic-replace behavior needs a real filesystem and is covered by
    # tests/component/test_runtime_store.py; this only proves the same
    # input-validation guard as `serialize_run_record` before any I/O.
    with pytest.raises(TypeError, match="record must be RunRecord"):
        persist_run_record(cast(RunRecord, {"not": "a run record"}))


def test_serialize_run_record_matches_the_golden_minimal_fixture() -> None:
    payload = serialize_run_record(_minimal_run_record())

    expected = (
        b'{"schema_version": 1, "run_id": "run-001", '
        b'"artifact_path": "/workspaces/opencode-tools/.opencode-tools/runs/'
        b'run-001/run.json", '
        b'"workspace": {"root": "/workspaces/opencode-tools"}, '
        b'"target": {"root": "/workspaces/opencode-tools/backend", '
        b'"workspace_relative": "backend", '
        b'"git_common_dir": "/workspaces/opencode-tools/backend/.git"}, '
        b'"issue_number": 4, "config": {}, "environment": {}, '
        b'"started_at": "2026-09-11T14:23:45.123456Z", '
        b'"current_phase": "PREFLIGHT", "persistence_status": "OK", '
        b'"issue_locator": null, "issue_ref": null, "finished_at": null, '
        b'"duration_ns": null, "review_cycle": null, "provider_attempt": null, '
        b'"terminal_outcome": null, "git_baseline": null, "git_checks": [], '
        b'"git_postflight": null, "git_safety_status": null, "timeline": [], '
        b'"attempts": [], "errors": [], "last_successful_sequence": null, '
        b'"artifact_incomplete": false, "trigger_outcome": null, '
        b'"final_status": null, "expected_exit_code": null, '
        b'"changes_preserved": false, "termination_confirmed": null}\n'
    )

    assert payload == expected


def test_serialize_run_record_is_utf8_with_exactly_one_trailing_newline() -> None:
    payload = serialize_run_record(_minimal_run_record())

    text = payload.decode("utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert text.count("\n") == 1


def test_serialize_run_record_is_stable_across_repeated_calls() -> None:
    record = _full_run_record()

    assert serialize_run_record(record) == serialize_run_record(record)
    assert serialize_run_record(record) == serialize_run_record(_full_run_record())


def test_serialize_run_record_round_trips_through_json(tmp_path: Path) -> None:
    record = _full_run_record()
    payload = serialize_run_record(record)

    path = tmp_path / "run.json"
    path.write_bytes(payload)
    parsed = json.loads(path.read_bytes())

    assert parsed == to_primitive(record)


def test_serialize_run_record_represents_every_canonical_group() -> None:
    parsed = json.loads(serialize_run_record(_full_run_record()))

    # input
    assert parsed["issue_number"] == 4
    assert parsed["issue_locator"]["repository_identity"]["owner"] == "example"
    assert parsed["issue_ref"]["url"].endswith("/issues/4")
    assert parsed["workspace"]["root"] == str(WORKSPACE_ROOT)
    assert parsed["target"]["root"] == str(TARGET_ROOT)
    # config
    assert parsed["config"] == {"source": "defaults"}
    # environment
    assert parsed["environment"]["opencode_version"] == "1.17.18"
    # timing
    assert parsed["started_at"] == "2026-09-11T14:23:45.123456Z"
    assert parsed["finished_at"] == "2026-09-11T14:24:01.654321Z"
    assert parsed["duration_ns"] == 15_865_000
    # state
    assert parsed["current_phase"] == "FINISHED"
    assert parsed["review_cycle"] == 1
    assert parsed["provider_attempt"] == 1
    assert parsed["terminal_outcome"] == "SUCCEEDED"
    # git
    assert parsed["git_baseline"]["branch"] == "feat/m08-runtime-store"
    assert len(parsed["git_checks"]) == 1
    assert parsed["git_postflight"]["purpose"] == "postflight"
    assert parsed["git_safety_status"] == "SAFE"
    # timeline
    assert parsed["timeline"] == [
        {"sequence": 0, "phase": "PREFLIGHT", "event": "run_initialized"}
    ]
    # attempts
    assert len(parsed["attempts"]) == 1
    assert parsed["attempts"][0]["logical_invocation_id"] == "coder-cycle-1"
    # errors
    assert len(parsed["errors"]) == 2
    # persistence
    assert parsed["last_successful_sequence"] == 3
    assert parsed["artifact_incomplete"] is True
    # result
    assert parsed["trigger_outcome"] == "GIT_SAFETY_ERROR"
    assert parsed["final_status"] == "FAILED"
    assert parsed["expected_exit_code"] == 10
    assert parsed["changes_preserved"] is True
    assert parsed["termination_confirmed"] is True


def test_serialize_run_record_keeps_the_status_dimensions_distinct() -> None:
    parsed = json.loads(serialize_run_record(_full_run_record()))

    assert parsed["current_phase"] != parsed["final_status"]
    assert parsed["terminal_outcome"] != parsed["trigger_outcome"]
    assert {
        parsed["current_phase"],
        parsed["git_safety_status"],
        parsed["final_status"],
        parsed["trigger_outcome"],
    } == {"FINISHED", "SAFE", "FAILED", "GIT_SAFETY_ERROR"}


def test_serialize_run_record_preserves_multiple_ordered_errors() -> None:
    parsed = json.loads(serialize_run_record(_full_run_record()))

    sequences = [error["sequence"] for error in parsed["errors"]]
    codes = [error["code"] for error in parsed["errors"]]
    assert sequences == [4, 5]
    assert codes == ["git.branch_drift", "git.uncommitted_change"]


def test_serialize_run_record_never_leaks_a_credential_or_full_environment() -> None:
    app_config = AppConfig(
        source=ConfigSource.DEFAULTS,
        execution=ExecutionConfig(
            opencode_timeout_seconds=1800,
            utility_timeout_seconds=30,
            termination_grace_seconds=5,
            max_review_cycles=3,
        ),
        provider_retry=ProviderRetryConfig(
            max_attempts=3,
            initial_delay_seconds=2,
            multiplier=2.0,
            max_delay_seconds=30,
        ),
        runtime_root=WORKSPACE_ROOT / ".opencode-tools",
    )
    sanitized = sanitize_app_config(app_config)
    assert set(sanitized) == {
        "source",
        "execution",
        "provider_retry",
        "runtime_root",
        "github_targets",
    }

    record = replace(
        _minimal_run_record(),
        config=cast(dict[str, FrozenJsonValue], sanitized),
    )

    text = serialize_run_record(record).decode("utf-8").lower()
    for forbidden in ("credential", "password", "secret", "api_key", "authorization"):
        assert forbidden not in text

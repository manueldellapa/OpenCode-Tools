"""Unit tests for the immutable domain contracts."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, is_dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

import pytest

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    AppConfig,
    AttemptRecord,
    ConfigSource,
    ErrorRecord,
    ExecutionConfig,
    FinalStatus,
    FrozenJsonValue,
    GitCheckRecord,
    GithubTargetOverride,
    GitSafetyStatus,
    GitState,
    IssueLocator,
    IssueRef,
    IssueResult,
    JsonConvertible,
    JsonValue,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    ProcessSpec,
    ProviderDiagnostic,
    ProviderRetryConfig,
    RepositoryIdentity,
    RetryDecision,
    ReviewStatus,
    RunOutcome,
    RunRecord,
    RunRequest,
    TargetRepository,
    Workspace,
    to_primitive,
)

NOW = datetime(2026, 9, 13, 8, 9, 10, 123456, tzinfo=UTC)
LATER = datetime(2026, 9, 13, 8, 9, 11, 654321, tzinfo=UTC)
WORKSPACE_ROOT = Path("/workspaces/opencode-tools")
TARGET_ROOT = WORKSPACE_ROOT / "backend"
ARTIFACT_PATH = WORKSPACE_ROOT / ".opencode-tools/runs/run-001/run.json"


def _workspace() -> Workspace:
    return Workspace(root=WORKSPACE_ROOT)


def _target() -> TargetRepository:
    return TargetRepository(
        root=TARGET_ROOT,
        workspace_relative=Path("backend"),
        git_common_dir=TARGET_ROOT / ".git",
    )


def _run_request() -> RunRequest:
    return RunRequest(issue_number=4, workspace=_workspace(), target_root=TARGET_ROOT)


def _execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        opencode_timeout_seconds=1800,
        utility_timeout_seconds=30,
        termination_grace_seconds=5,
        max_review_cycles=3,
    )


def _provider_retry_config() -> ProviderRetryConfig:
    return ProviderRetryConfig(
        max_attempts=3,
        initial_delay_seconds=2,
        multiplier=2.0,
        max_delay_seconds=30,
    )


def _app_config() -> AppConfig:
    return AppConfig(
        source=ConfigSource.DEFAULTS,
        execution=_execution_config(),
        provider_retry=_provider_retry_config(),
        runtime_root=WORKSPACE_ROOT / ".opencode-tools",
    )


def _identity() -> RepositoryIdentity:
    return RepositoryIdentity(
        host="github.com",
        owner="example",
        repository="backend",
        source="origin",
        remote_name="origin",
    )


def _locator() -> IssueLocator:
    return IssueLocator(repository_identity=_identity(), number=4)


def _issue_ref() -> IssueRef:
    return IssueRef(
        locator=_locator(),
        url="https://github.com/example/backend/issues/4",
        title="Define immutable domain values",
    )


def _process_spec(
    *,
    argv: tuple[str, ...] = ("/usr/bin/git", "status"),
    environment_overrides: dict[str, str] | None = None,
) -> ProcessSpec:
    return ProcessSpec(
        argv=argv,
        cwd=TARGET_ROOT,
        stdin=None,
        timeout_seconds=30,
        termination_grace_seconds=5,
        environment_overrides=environment_overrides or {},
    )


def _process_result(
    *,
    return_code: int | None = 0,
    timed_out: bool = False,
    termination_confirmed: bool | None = True,
    outcome: RunOutcome = RunOutcome.SUCCEEDED,
) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/git", "status"),
        cwd=TARGET_ROOT,
        started_at=NOW,
        finished_at=LATER,
        duration_ns=1_530_865_000,
        return_code=return_code,
        timed_out=timed_out,
        termination_confirmed=termination_confirmed,
        log_path=Path("reviewer-cycle-2-provider-attempt-3.log"),
        stdout_byte_count=4,
        stdout_sha256="stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="stderr-digest",
        outcome=outcome,
    )


def _provider_diagnostic() -> ProviderDiagnostic:
    return ProviderDiagnostic(
        source="session.error",
        signature="http-502",
        retryable=True,
        status_code=502,
        code="provider_unavailable",
    )


def _retry_decision() -> RetryDecision:
    return RetryDecision(
        should_retry=True,
        next_provider_attempt=2,
        planned_delay_seconds=2.0,
        retry_suppressed_due_to_target_change=False,
    )


def _parsed_response() -> ParsedAgentResponse:
    return ParsedAgentResponse(
        role=AgentRole.REVIEWER,
        body="The implementation meets the issue acceptance criteria.",
        review_status=ReviewStatus.APPROVED,
    )


def _agent_result() -> AgentResult:
    return AgentResult(
        role=AgentRole.REVIEWER,
        phase=PipelinePhase.REVIEWER,
        review_cycle=2,
        provider_attempt=3,
        process=_process_result(),
        terminal_response=_parsed_response(),
        session_id="session-001",
        verified_agent="reviewer",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )


def _git_state() -> GitState:
    return GitState(
        root=TARGET_ROOT,
        branch="feat/domain",
        head="0123456789abcdef",
        porcelain_summary="1 tracked file changed",
        staged=(),
        unstaged=("src/opencode_tools/domain.py",),
        untracked=("tests/unit/test_domain.py",),
        fingerprint="fingerprint-001",
    )


def _git_check(*, sequence: int = 0, purpose: str = "attempt-before") -> GitCheckRecord:
    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=(_process_result(),),
        state=_git_state(),
        safety_status=GitSafetyStatus.SAFE,
        compared_to="baseline",
    )


def _attempt_record() -> AttemptRecord:
    return AttemptRecord(
        logical_invocation_id="reviewer-cycle-2",
        role=AgentRole.REVIEWER,
        review_cycle=2,
        provider_attempt=3,
        git_before=_git_check(sequence=1, purpose="attempt-before"),
        git_after=_git_check(sequence=2, purpose="attempt-after"),
        agent_result=_agent_result(),
        retry_decision=False,
    )


def _error_record() -> ErrorRecord:
    return ErrorRecord(
        sequence=3,
        timestamp=LATER,
        phase=PipelinePhase.POSTFLIGHT,
        outcome=RunOutcome.GIT_SAFETY_ERROR,
        code="git.branch_drift",
        message="The target branch changed during the run.",
        related_record="git-check-2",
        technical_detail="expected feat/domain, observed main",
    )


def _run_record(
    *,
    config: dict[str, FrozenJsonValue] | None = None,
    timeline: tuple[dict[str, FrozenJsonValue], ...] | None = None,
) -> RunRecord:
    effective_config: dict[str, FrozenJsonValue] = config or {
        "source": "defaults",
        "lifecycle": {"max_review_cycles": 3},
        "roles": ("architect", "coder", "reviewer"),
    }
    run_timeline: tuple[dict[str, FrozenJsonValue], ...] = timeline or (
        {
            "sequence": 0,
            "phase": "PREFLIGHT",
            "event": "run_initialized",
        },
    )
    return RunRecord(
        schema_version=1,
        run_id="run-001",
        artifact_path=ARTIFACT_PATH,
        workspace=_workspace(),
        target=_target(),
        issue_number=4,
        config=effective_config,
        environment={
            "tool_version": "0.1.0",
            "python_version": "3.13.15",
            "opencode_version": None,
        },
        started_at=NOW,
        current_phase=PipelinePhase.FINISHED,
        persistence_status=PersistenceStatus.OK,
        issue_locator=_locator(),
        issue_ref=_issue_ref(),
        finished_at=LATER,
        duration_ns=1_530_865_000,
        review_cycle=2,
        provider_attempt=3,
        terminal_outcome=RunOutcome.SUCCEEDED,
        git_baseline=_git_state(),
        git_checks=(_git_check(),),
        git_postflight=_git_check(sequence=4, purpose="postflight"),
        git_safety_status=GitSafetyStatus.SAFE,
        timeline=run_timeline,
        attempts=(_attempt_record(),),
        errors=(),
        last_successful_sequence=4,
        artifact_incomplete=False,
        trigger_outcome=RunOutcome.SUCCEEDED,
        final_status=FinalStatus.APPROVED,
        expected_exit_code=0,
        changes_preserved=True,
        termination_confirmed=True,
    )


def _issue_result() -> IssueResult:
    return IssueResult(
        run_id="run-001",
        artifact_path=ARTIFACT_PATH,
        final_status=FinalStatus.APPROVED,
        expected_exit_code=0,
        trigger_outcome=RunOutcome.SUCCEEDED,
        git_safety_status=GitSafetyStatus.SAFE,
        persistence_status=PersistenceStatus.OK,
        changes_preserved=True,
        termination_confirmed=True,
    )


def _record_samples() -> tuple[tuple[object, str], ...]:
    return (
        (_workspace(), "root"),
        (_target(), "root"),
        (_identity(), "host"),
        (_locator(), "number"),
        (_issue_ref(), "title"),
        (_process_spec(), "argv"),
        (_process_result(), "duration_ns"),
        (_provider_diagnostic(), "source"),
        (_retry_decision(), "should_retry"),
        (_parsed_response(), "body"),
        (_agent_result(), "provider_attempt"),
        (_git_state(), "branch"),
        (_git_check(), "purpose"),
        (_attempt_record(), "retry_decision"),
        (_error_record(), "message"),
        (_run_record(), "current_phase"),
        (_issue_result(), "final_status"),
    )


def test_canonical_str_enum_members_and_values() -> None:
    expected: dict[type[StrEnum], tuple[str, ...]] = {
        AgentRole: ("ARCHITECT", "CODER", "REVIEWER"),
        PipelinePhase: (
            "PREFLIGHT",
            "ARCHITECT",
            "CODER",
            "REVIEWER",
            "POSTFLIGHT",
            "FINALIZATION",
            "FINISHED",
        ),
        AgentStatus: ("READY", "COMPLETED", "FAILED"),
        ReviewStatus: ("APPROVED", "CHANGES_REQUIRED"),
        RunOutcome: (
            "SUCCEEDED",
            "CONFIG_ERROR",
            "PREFLIGHT_ERROR",
            "PROVIDER_ERROR",
            "TIMEOUT",
            "PROCESS_ERROR",
            "PROTOCOL_ERROR",
            "AGENT_REPORTED_FAILURE",
            "REVIEW_CYCLES_EXHAUSTED",
            "GIT_SAFETY_ERROR",
            "LOGGING_ERROR",
            "INTERRUPTED",
        ),
        FinalStatus: ("APPROVED", "FAILED"),
        GitSafetyStatus: ("SAFE", "UNSAFE", "INDETERMINATE"),
        PersistenceStatus: ("OK", "FAILED", "INCOMPLETE"),
    }

    for enum_type, values in expected.items():
        assert tuple(member.name for member in enum_type) == values
        assert tuple(member.value for member in enum_type) == values
        assert all(isinstance(member, StrEnum) for member in enum_type)
        with pytest.raises(ValueError):
            enum_type("UNKNOWN")


def test_status_dimensions_remain_distinct_and_guard_cross_enum_values() -> None:
    assert "CHANGES_REQUIRED" in ReviewStatus.__members__
    assert "CHANGES_REQUIRED" not in RunOutcome.__members__
    assert "CHANGES_REQUIRED" not in FinalStatus.__members__
    assert "PREFLIGHT_ERROR" in RunOutcome.__members__
    assert "INTERRUPTED" in RunOutcome.__members__

    with pytest.raises(TypeError, match="review_status must be ReviewStatus"):
        ParsedAgentResponse(
            role=AgentRole.REVIEWER,
            body="approved",
            review_status=cast(ReviewStatus, FinalStatus.APPROVED),
        )

    with pytest.raises(TypeError, match="final_status must be FinalStatus"):
        IssueResult(
            run_id="run-001",
            artifact_path=ARTIFACT_PATH,
            final_status=cast(FinalStatus, ReviewStatus.APPROVED),
            expected_exit_code=0,
            trigger_outcome=RunOutcome.SUCCEEDED,
            git_safety_status=GitSafetyStatus.SAFE,
            persistence_status=PersistenceStatus.OK,
            changes_preserved=True,
        )


def test_all_named_domain_records_are_frozen_and_slotted() -> None:
    for record, field_name in _record_samples():
        assert is_dataclass(record)
        assert not hasattr(record, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(record, field_name, getattr(record, field_name))


def test_workspace_and_target_apply_only_pure_path_invariants() -> None:
    assert Workspace(Path("/path/that/does/not/need/to/exist")).root.is_absolute()
    assert TargetRepository(
        root=Path("/workspace"),
        workspace_relative=Path("."),
        git_common_dir=Path("/workspace/.git"),
    ).workspace_relative == Path(".")

    with pytest.raises(ValueError, match="root must be absolute"):
        Workspace(Path("relative"))
    with pytest.raises(ValueError, match="workspace_relative must be relative"):
        TargetRepository(
            root=Path("/workspace/backend"),
            workspace_relative=Path("/absolute"),
            git_common_dir=Path("/workspace/backend/.git"),
        )
    with pytest.raises(ValueError, match="must not contain '..'"):
        TargetRepository(
            root=Path("/workspace/backend"),
            workspace_relative=Path("../backend"),
            git_common_dir=Path("/workspace/backend/.git"),
        )


def test_run_request_requires_a_positive_issue_and_contained_target() -> None:
    request = _run_request()
    assert is_dataclass(request)
    assert not hasattr(request, "__dict__")
    field_name = "issue_number"
    with pytest.raises(FrozenInstanceError):
        setattr(request, field_name, getattr(request, field_name))

    assert (
        RunRequest(
            issue_number=4,
            workspace=_workspace(),
            target_root=WORKSPACE_ROOT,
        ).target_root
        == WORKSPACE_ROOT
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        RunRequest(
            issue_number=cast(int, True),
            workspace=_workspace(),
            target_root=TARGET_ROOT,
        )
    with pytest.raises(ValueError, match="issue_number must be at least 1"):
        RunRequest(issue_number=0, workspace=_workspace(), target_root=TARGET_ROOT)
    with pytest.raises(TypeError, match="workspace must be Workspace"):
        RunRequest(
            issue_number=4,
            workspace=cast(Workspace, WORKSPACE_ROOT),
            target_root=TARGET_ROOT,
        )
    with pytest.raises(ValueError, match="target_root must be absolute"):
        RunRequest(
            issue_number=4,
            workspace=_workspace(),
            target_root=Path("relative/backend"),
        )
    with pytest.raises(ValueError, match="target_root must be contained in workspace"):
        RunRequest(
            issue_number=4,
            workspace=_workspace(),
            target_root=Path("/elsewhere/backend"),
        )


def test_execution_and_provider_retry_config_apply_v1_ranges() -> None:
    execution = _execution_config()
    assert is_dataclass(execution)
    assert not hasattr(execution, "__dict__")
    execution_field = "max_review_cycles"
    with pytest.raises(FrozenInstanceError):
        setattr(execution, execution_field, getattr(execution, execution_field))

    with pytest.raises(TypeError, match="opencode_timeout_seconds must be a finite"):
        ExecutionConfig(
            opencode_timeout_seconds=cast(float, True),
            utility_timeout_seconds=30,
            termination_grace_seconds=5,
            max_review_cycles=3,
        )
    with pytest.raises(ValueError, match="opencode_timeout_seconds must be <=7200"):
        ExecutionConfig(
            opencode_timeout_seconds=7201,
            utility_timeout_seconds=30,
            termination_grace_seconds=5,
            max_review_cycles=3,
        )
    with pytest.raises(TypeError, match="max_review_cycles must be an integer"):
        ExecutionConfig(
            opencode_timeout_seconds=1800,
            utility_timeout_seconds=30,
            termination_grace_seconds=5,
            max_review_cycles=cast(int, 3.0),
        )
    with pytest.raises(ValueError, match="max_review_cycles must be at most 20"):
        ExecutionConfig(
            opencode_timeout_seconds=1800,
            utility_timeout_seconds=30,
            termination_grace_seconds=5,
            max_review_cycles=21,
        )

    provider_retry = _provider_retry_config()
    assert is_dataclass(provider_retry)
    assert not hasattr(provider_retry, "__dict__")
    provider_retry_field = "max_attempts"
    with pytest.raises(FrozenInstanceError):
        setattr(
            provider_retry,
            provider_retry_field,
            getattr(provider_retry, provider_retry_field),
        )

    with pytest.raises(ValueError, match=r"multiplier must be >1\.0"):
        ProviderRetryConfig(
            max_attempts=3,
            initial_delay_seconds=2,
            multiplier=1.0,
            max_delay_seconds=30,
        )
    with pytest.raises(ValueError, match="max_delay_seconds must be >=10"):
        ProviderRetryConfig(
            max_attempts=3,
            initial_delay_seconds=10,
            multiplier=2.0,
            max_delay_seconds=5,
        )


def test_app_config_requires_typed_sections_and_a_canonical_runtime_root() -> None:
    config = _app_config()
    assert is_dataclass(config)
    assert not hasattr(config, "__dict__")
    assert config.source is ConfigSource.DEFAULTS
    assert config.github_targets == ()
    config_field = "runtime_root"
    with pytest.raises(FrozenInstanceError):
        setattr(config, config_field, getattr(config, config_field))

    with pytest.raises(TypeError, match="execution must be ExecutionConfig"):
        AppConfig(
            source=ConfigSource.DEFAULTS,
            execution=cast(ExecutionConfig, None),
            provider_retry=_provider_retry_config(),
            runtime_root=WORKSPACE_ROOT / ".opencode-tools",
        )
    with pytest.raises(TypeError, match="provider_retry must be ProviderRetryConfig"):
        AppConfig(
            source=ConfigSource.DEFAULTS,
            execution=_execution_config(),
            provider_retry=cast(ProviderRetryConfig, None),
            runtime_root=WORKSPACE_ROOT / ".opencode-tools",
        )
    with pytest.raises(ValueError, match="runtime_root must be absolute"):
        AppConfig(
            source=ConfigSource.DEFAULTS,
            execution=_execution_config(),
            provider_retry=_provider_retry_config(),
            runtime_root=Path(".opencode-tools"),
        )
    with pytest.raises(ValueError, match="must not be under Git metadata"):
        AppConfig(
            source=ConfigSource.DEFAULTS,
            execution=_execution_config(),
            provider_retry=_provider_retry_config(),
            runtime_root=WORKSPACE_ROOT / ".git" / "opencode-tools",
        )
    with pytest.raises(ValueError, match="github_targets contains a duplicate"):
        AppConfig(
            source=ConfigSource.DEFAULTS,
            execution=_execution_config(),
            provider_retry=_provider_retry_config(),
            runtime_root=WORKSPACE_ROOT / ".opencode-tools",
            github_targets=(
                GithubTargetOverride(
                    workspace_relative=Path("backend"),
                    remote="origin",
                ),
                GithubTargetOverride(
                    workspace_relative=Path("./backend"),
                    repository="github.com/example/backend",
                ),
            ),
        )


def test_github_target_override_requires_a_remote_or_repository() -> None:
    override = GithubTargetOverride(workspace_relative=Path("backend"), remote="origin")
    assert is_dataclass(override)
    assert not hasattr(override, "__dict__")
    assert override.repository is None

    with pytest.raises(ValueError, match="workspace_relative must not contain '..'"):
        GithubTargetOverride(workspace_relative=Path("../backend"), remote="origin")
    with pytest.raises(ValueError, match="remote must not contain control characters"):
        GithubTargetOverride(workspace_relative=Path("backend"), remote="ori\x01gin")
    with pytest.raises(
        ValueError,
        match="repository must be 'owner/repo' or 'host/owner/repo'",
    ):
        GithubTargetOverride(workspace_relative=Path("backend"), repository="backend")
    with pytest.raises(
        ValueError,
        match="at least one of remote or repository is required",
    ):
        GithubTargetOverride(workspace_relative=Path("backend"))


def test_repository_and_issue_identity_invariants() -> None:
    assert _issue_ref().schema_version == 1

    with pytest.raises(TypeError, match="number must be an integer"):
        IssueLocator(_identity(), cast(int, True))
    with pytest.raises(ValueError, match="number must be at least 1"):
        IssueLocator(_identity(), 0)
    with pytest.raises(ValueError, match="title must not be empty"):
        IssueRef(
            locator=_locator(),
            url="https://github.com/example/backend/issues/4",
            title="  ",
        )
    with pytest.raises(ValueError, match="canonical HTTPS URL"):
        IssueRef(
            locator=_locator(),
            url="http://github.com/example/backend/issues/4",
            title="Issue",
        )
    with pytest.raises(ValueError, match="canonical HTTPS URL"):
        IssueRef(
            locator=_locator(),
            url="https://github.com:8443/example/backend/issues/4",
            title="Issue",
        )
    with pytest.raises(ValueError, match="canonical HTTPS URL"):
        IssueRef(
            locator=_locator(),
            url="https://github.com/example/backend/issues/4?ref=pr",
            title="Issue",
        )
    with pytest.raises(ValueError, match="must not contain control characters"):
        IssueRef(
            locator=_locator(),
            url="https://github.com/example/backend/issues/4\n",
            title="Issue",
        )
    with pytest.raises(ValueError, match="must not contain control characters"):
        IssueRef(
            locator=_locator(),
            url="https://github.com/example/backend/issues/4",
            title="Issue\x00",
        )
    with pytest.raises(ValueError, match="exceeds the defensive length limit"):
        IssueRef(
            locator=_locator(),
            url="https://github.com/example/backend/issues/4",
            title="x" * 2001,
        )
    with pytest.raises(ValueError, match="schema_version must be 1"):
        IssueRef(
            locator=_locator(),
            url="https://github.com/example/backend/issues/4",
            title="Issue",
            schema_version=2,
        )


def test_positive_counters_and_non_negative_measurements() -> None:
    with pytest.raises(ValueError, match="provider_attempt must be at least 1"):
        AgentResult(
            role=AgentRole.ARCHITECT,
            phase=PipelinePhase.ARCHITECT,
            review_cycle=None,
            provider_attempt=0,
            process=_process_result(),
            terminal_response=None,
            session_id=None,
            verified_agent=None,
            provider_diagnostic=None,
            outcome=RunOutcome.PROCESS_ERROR,
        )
    with pytest.raises(ValueError, match="review_cycle must be at least 1"):
        AgentResult(
            role=AgentRole.CODER,
            phase=PipelinePhase.CODER,
            review_cycle=0,
            provider_attempt=1,
            process=_process_result(),
            terminal_response=None,
            session_id=None,
            verified_agent=None,
            provider_diagnostic=None,
            outcome=RunOutcome.PROCESS_ERROR,
        )
    with pytest.raises(ValueError, match="duration_ns must be at least 0"):
        ProcessResult(
            command=("/usr/bin/false",),
            cwd=TARGET_ROOT,
            started_at=NOW,
            finished_at=LATER,
            duration_ns=-1,
            return_code=1,
            timed_out=False,
            termination_confirmed=True,
            log_path=Path("attempt.log"),
            stdout_byte_count=0,
            stdout_sha256="stdout",
            stderr_byte_count=0,
            stderr_sha256="stderr",
            outcome=RunOutcome.PROCESS_ERROR,
        )


def test_retry_decision_requires_consistent_retry_fields() -> None:
    with pytest.raises(ValueError, match="next_provider_attempt must be at least 2"):
        RetryDecision(
            should_retry=True,
            next_provider_attempt=1,
            planned_delay_seconds=2.0,
            retry_suppressed_due_to_target_change=False,
        )
    with pytest.raises(
        ValueError, match="planned_delay_seconds must be greater than zero"
    ):
        RetryDecision(
            should_retry=True,
            next_provider_attempt=2,
            planned_delay_seconds=0,
            retry_suppressed_due_to_target_change=False,
        )
    with pytest.raises(
        ValueError,
        match="retry_suppressed_due_to_target_change must be False",
    ):
        RetryDecision(
            should_retry=True,
            next_provider_attempt=2,
            planned_delay_seconds=2.0,
            retry_suppressed_due_to_target_change=True,
        )
    with pytest.raises(
        ValueError,
        match="next_provider_attempt must be None when should_retry is False",
    ):
        RetryDecision(
            should_retry=False,
            next_provider_attempt=2,
            planned_delay_seconds=None,
            retry_suppressed_due_to_target_change=False,
        )
    with pytest.raises(
        ValueError,
        match="planned_delay_seconds must be None when should_retry is False",
    ):
        RetryDecision(
            should_retry=False,
            next_provider_attempt=None,
            planned_delay_seconds=2.0,
            retry_suppressed_due_to_target_change=False,
        )

    denied = RetryDecision(
        should_retry=False,
        next_provider_attempt=None,
        planned_delay_seconds=None,
        retry_suppressed_due_to_target_change=True,
    )
    assert denied.next_provider_attempt is None


def test_timestamps_must_be_timezone_aware_utc() -> None:
    naive = datetime(2026, 9, 13, 8, 9, 10)  # noqa: DTZ001 - invalid fixture

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        ErrorRecord(
            sequence=0,
            timestamp=naive,
            phase=PipelinePhase.PREFLIGHT,
            outcome=RunOutcome.PREFLIGHT_ERROR,
            code="preflight.failed",
            message="Preflight failed.",
        )


def test_process_result_preserves_canonical_nullable_fields() -> None:
    result = _process_result(
        return_code=None,
        timed_out=True,
        termination_confirmed=None,
        outcome=RunOutcome.TIMEOUT,
    )

    assert result.return_code is None
    assert result.termination_confirmed is None
    assert result.timed_out is True
    assert result.outcome is RunOutcome.TIMEOUT
    primitive = to_primitive(result)
    assert json.loads(json.dumps(primitive)) == primitive
    assert isinstance(primitive, dict)
    assert primitive["return_code"] is None
    assert primitive["termination_confirmed"] is None

    interrupted = _process_result(
        return_code=None,
        termination_confirmed=False,
        outcome=RunOutcome.INTERRUPTED,
    )
    assert interrupted.outcome is RunOutcome.INTERRUPTED
    assert interrupted.timed_out is False


def test_process_result_rejects_non_process_outcomes_and_invalid_success() -> None:
    with pytest.raises(ValueError, match="not a process-level outcome"):
        _process_result(outcome=RunOutcome.PROVIDER_ERROR)
    with pytest.raises(ValueError, match="SUCCEEDED requires"):
        _process_result(return_code=None, termination_confirmed=None)
    with pytest.raises(ValueError, match="timed_out must match"):
        _process_result(timed_out=True, outcome=RunOutcome.PROCESS_ERROR)


def test_agent_dimensions_are_orthogonal_and_nullable_where_inapplicable() -> None:
    result = AgentResult(
        role=AgentRole.REVIEWER,
        phase=PipelinePhase.REVIEWER,
        review_cycle=2,
        provider_attempt=3,
        process=_process_result(
            return_code=1,
            termination_confirmed=True,
            outcome=RunOutcome.PROCESS_ERROR,
        ),
        terminal_response=None,
        session_id="session-provider-error",
        verified_agent=None,
        provider_diagnostic=_provider_diagnostic(),
        outcome=RunOutcome.PROVIDER_ERROR,
    )

    assert result.phase is PipelinePhase.REVIEWER
    assert result.review_cycle == 2
    assert result.provider_attempt == 3
    assert result.outcome is RunOutcome.PROVIDER_ERROR
    assert result.terminal_response is None


def test_agent_role_phase_and_cycle_are_coherent() -> None:
    with pytest.raises(ValueError, match="phase must match role"):
        AgentResult(
            role=AgentRole.ARCHITECT,
            phase=PipelinePhase.REVIEWER,
            review_cycle=None,
            provider_attempt=1,
            process=_process_result(),
            terminal_response=None,
            session_id=None,
            verified_agent=None,
            provider_diagnostic=None,
            outcome=RunOutcome.PROCESS_ERROR,
        )
    with pytest.raises(ValueError, match="require a review_cycle"):
        AgentResult(
            role=AgentRole.REVIEWER,
            phase=PipelinePhase.REVIEWER,
            review_cycle=None,
            provider_attempt=1,
            process=_process_result(),
            terminal_response=None,
            session_id=None,
            verified_agent=None,
            provider_diagnostic=None,
            outcome=RunOutcome.PROCESS_ERROR,
        )


def test_git_state_supports_partial_probe_and_defensively_copies_inventories() -> None:
    staged = ["staged.py"]
    unstaged = ["unstaged.py"]
    untracked = ["untracked.py"]
    state = GitState(
        root=TARGET_ROOT,
        branch=None,
        head=None,
        porcelain_summary="probe incomplete",
        staged=cast(tuple[str, ...], staged),
        unstaged=cast(tuple[str, ...], unstaged),
        untracked=cast(tuple[str, ...], untracked),
        fingerprint=None,
    )

    staged.append("later.py")
    unstaged.clear()
    untracked[0] = "changed.py"

    assert state.branch is None
    assert state.head is None
    assert state.fingerprint is None
    assert state.staged == ("staged.py",)
    assert state.unstaged == ("unstaged.py",)
    assert state.untracked == ("untracked.py",)

    with pytest.raises(ValueError, match="SAFE requires complete"):
        GitCheckRecord(
            sequence=0,
            purpose="failed-probe",
            process_results=(),
            state=state,
            safety_status=GitSafetyStatus.SAFE,
        )
    with pytest.raises(ValueError, match="must be git-state-v1"):
        GitState(
            root=TARGET_ROOT,
            branch="main",
            head="0123456789abcdef",
            porcelain_summary="clean",
            staged=(),
            unstaged=(),
            untracked=(),
            fingerprint="fingerprint",
            fingerprint_version="git-state-v2",
        )


def test_ordered_collections_reject_unordered_or_one_shot_inputs() -> None:
    with pytest.raises(TypeError, match="collection of strings"):
        GitState(
            root=TARGET_ROOT,
            branch="main",
            head="0123456789abcdef",
            porcelain_summary="dirty",
            staged=cast(tuple[str, ...], {"a.py", "b.py"}),
            unstaged=(),
            untracked=(),
            fingerprint="fingerprint",
        )


def test_sequence_inputs_are_defensively_copied_to_tuples() -> None:
    argv = ["/usr/bin/git", "status"]
    process_spec = _process_spec(argv=cast(tuple[str, ...], argv))
    argv.append("--short")
    assert process_spec.argv == ("/usr/bin/git", "status")

    checks = [_git_check()]
    attempts = [_attempt_record()]
    errors = [_error_record()]
    timeline: list[dict[str, FrozenJsonValue]] = [
        {"sequence": 0, "event": "run_initialized"}
    ]
    run = RunRecord(
        schema_version=1,
        run_id="run-defensive",
        artifact_path=ARTIFACT_PATH,
        workspace=_workspace(),
        target=_target(),
        issue_number=4,
        config={},
        environment={},
        started_at=NOW,
        current_phase=PipelinePhase.PREFLIGHT,
        persistence_status=PersistenceStatus.OK,
        git_checks=cast(tuple[GitCheckRecord, ...], checks),
        timeline=cast(
            tuple[dict[str, FrozenJsonValue], ...],
            timeline,
        ),
        attempts=cast(tuple[AttemptRecord, ...], attempts),
        errors=cast(tuple[ErrorRecord, ...], errors),
    )

    checks.clear()
    attempts.clear()
    errors.clear()
    timeline.clear()

    assert len(run.git_checks) == 1
    assert len(run.attempts) == 1
    assert len(run.errors) == 1
    assert len(run.timeline) == 1


def test_mapping_inputs_are_deeply_detached_and_immutable() -> None:
    nested_values = [1, 2]
    nested_mapping: dict[str, object] = {"values": nested_values}
    source: dict[str, object] = {"nested": nested_mapping}
    run = _run_record(
        config=cast(dict[str, FrozenJsonValue], source),
    )
    environment = {"MODE": "safe"}
    process_spec = _process_spec(environment_overrides=environment)

    nested_values.append(3)
    nested_mapping["new"] = "changed"
    source["later"] = True
    environment["MODE"] = "changed"

    primitive = to_primitive(run)
    assert isinstance(primitive, dict)
    assert primitive["config"] == {"nested": {"values": [1, 2]}}
    assert dict(process_spec.environment_overrides) == {"MODE": "safe"}

    with pytest.raises(TypeError):
        cast(dict[str, FrozenJsonValue], run.config)["new"] = True
    with pytest.raises(TypeError):
        cast(dict[str, str], process_spec.environment_overrides)["NEW"] = "x"


def test_partial_run_preserves_explicit_nullable_dimensions() -> None:
    run = RunRecord(
        schema_version=1,
        run_id="run-partial",
        artifact_path=ARTIFACT_PATH,
        workspace=_workspace(),
        target=_target(),
        issue_number=4,
        config={},
        environment={},
        started_at=NOW,
        current_phase=PipelinePhase.PREFLIGHT,
        persistence_status=PersistenceStatus.OK,
    )

    assert run.issue_locator is None
    assert run.issue_ref is None
    assert run.finished_at is None
    assert run.duration_ns is None
    assert run.review_cycle is None
    assert run.provider_attempt is None
    assert run.terminal_outcome is None
    assert run.git_safety_status is None
    assert run.final_status is None
    assert run.expected_exit_code is None
    assert run.termination_confirmed is None

    timestamp_only = replace(run, finished_at=LATER)
    duration_only = replace(run, duration_ns=1)
    assert timestamp_only.duration_ns is None
    assert duration_only.finished_at is None

    primitive = to_primitive(run)
    assert json.loads(json.dumps(primitive)) == primitive
    assert isinstance(primitive, dict)
    for nullable_field in (
        "finished_at",
        "duration_ns",
        "review_cycle",
        "provider_attempt",
        "terminal_outcome",
        "git_safety_status",
        "final_status",
        "expected_exit_code",
        "termination_confirmed",
    ):
        assert primitive[nullable_field] is None


def test_final_status_approval_requires_a_green_git_and_persistence_gate() -> None:
    approved_run = RunRecord(
        schema_version=1,
        run_id="run-gate",
        artifact_path=ARTIFACT_PATH,
        workspace=_workspace(),
        target=_target(),
        issue_number=4,
        config={},
        environment={},
        started_at=NOW,
        current_phase=PipelinePhase.FINISHED,
        persistence_status=PersistenceStatus.OK,
        final_status=FinalStatus.APPROVED,
        git_safety_status=GitSafetyStatus.SAFE,
        expected_exit_code=0,
    )

    with pytest.raises(ValueError, match="APPROVED requires a SAFE git_safety_status"):
        replace(approved_run, git_safety_status=GitSafetyStatus.UNSAFE)
    with pytest.raises(ValueError, match="APPROVED requires persistence_status OK"):
        replace(approved_run, persistence_status=PersistenceStatus.FAILED)
    with pytest.raises(ValueError, match="APPROVED requires expected_exit_code 0"):
        replace(approved_run, expected_exit_code=20)
    with pytest.raises(ValueError, match="FAILED must not use expected_exit_code 0"):
        replace(approved_run, final_status=FinalStatus.FAILED, expected_exit_code=0)

    approved_issue = _issue_result()

    with pytest.raises(ValueError, match="APPROVED requires a SAFE git_safety_status"):
        replace(approved_issue, git_safety_status=GitSafetyStatus.UNSAFE)
    with pytest.raises(ValueError, match="APPROVED requires persistence_status OK"):
        replace(approved_issue, persistence_status=PersistenceStatus.FAILED)
    with pytest.raises(ValueError, match="APPROVED requires expected_exit_code 0"):
        replace(approved_issue, expected_exit_code=20)
    with pytest.raises(ValueError, match="FAILED must not use expected_exit_code 0"):
        replace(approved_issue, final_status=FinalStatus.FAILED, expected_exit_code=0)


def test_error_records_cannot_report_success() -> None:
    with pytest.raises(ValueError, match="must describe an error"):
        ErrorRecord(
            sequence=0,
            timestamp=NOW,
            phase=PipelinePhase.FINALIZATION,
            outcome=RunOutcome.SUCCEEDED,
            code="invalid.success",
            message="This is not an error.",
        )


def _assert_json_tree(value: JsonValue) -> None:
    if value is None or type(value) in (bool, int, float, str):
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_tree(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            _assert_json_tree(item)
        return
    raise AssertionError(f"non-JSON value: {value!r}")


def test_to_primitive_recursively_converts_and_detaches_domain_values() -> None:
    run = _run_record()
    primitive = to_primitive(run)

    _assert_json_tree(primitive)
    assert isinstance(primitive, dict)
    assert primitive["current_phase"] == "FINISHED"
    assert primitive["final_status"] == "APPROVED"
    assert primitive["artifact_path"] == str(ARTIFACT_PATH)
    assert primitive["started_at"] == "2026-09-13T08:09:10.123456Z"
    assert primitive["attempts"] and isinstance(primitive["attempts"], list)

    config = primitive["config"]
    assert isinstance(config, dict)
    config["source"] = "mutated"
    fresh_primitive = to_primitive(run)
    assert isinstance(fresh_primitive, dict)
    assert fresh_primitive["config"] != config


def test_every_persistable_record_produces_json_native_values() -> None:
    for record, _ in _record_samples():
        if isinstance(record, ProcessSpec):
            continue
        primitive = to_primitive(cast(JsonConvertible, record))
        _assert_json_tree(primitive)
        assert (
            json.loads(json.dumps(primitive, allow_nan=False, ensure_ascii=False))
            == primitive
        )


def test_primitive_conversion_fails_closed_for_unsupported_values() -> None:
    with pytest.raises(TypeError, match="ProcessSpec contains runtime-only values"):
        to_primitive(cast(JsonConvertible, _process_spec()))
    with pytest.raises(TypeError, match="keys must be strings"):
        to_primitive(cast(JsonConvertible, {1: "invalid"}))
    with pytest.raises(TypeError, match="keys must be strings"):
        to_primitive(cast(JsonConvertible, {AgentRole.CODER: "invalid"}))
    with pytest.raises(TypeError, match="unsupported JSON value"):
        to_primitive(cast(JsonConvertible, object()))
    with pytest.raises(ValueError, match="non-finite"):
        to_primitive(float("nan"))

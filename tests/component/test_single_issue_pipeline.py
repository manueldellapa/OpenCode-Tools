"""Component tests for `orchestrator.run_issue_pipeline` (M13-02, M13-03).

Drives the full nominal-through-rework path against an `IssueOrchestrator`
built directly from deterministic port fakes -- a `SequencedAgentRunner`,
`SequencedGitSafetyPort`, a recording `RunStorePort`, and an
`OpenCodePreflightPort` fake whose `recheck` always succeeds -- using real
`tmp_path`-derived absolute paths for `Workspace`/`TargetRepository`, exactly
as a composed run would see them. No concrete adapter, no subprocess, no
real GitHub or OpenCode, no real sleep. Proves the canonical invocation order
and its preconditions (architect `READY` with a locator-consistent
`ISSUE_REF_JSON` before the coder; coder `COMPLETED` before the reviewer),
that opaque payloads (handoff, coder report, reviewer feedback, Git change
inventory) reach the next role's prompt byte-for-byte, that a read-only
role's own Git mutation blocks the next phase exactly as a protocol failure
would, that a reviewer `CHANGES_REQUIRED` below `max_review_cycles` composes
the next coder/reviewer cycle in the same loop while cycle exhaustion never
invokes one more coder, and that the provider-retry loop `IssueOrchestrator.
run_provider_attempts` already owns (M12, unchanged) -- recovery,
exhaustion, an untrusted diagnostic, and a coder's own target-change
suppression -- composes correctly through every role without ever calling a
fourth agent or acting on a reviewer's verdict as a final status (that
remains M13-04).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    IssueLocator,
    IssueRef,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    ProviderDiagnostic,
    ProviderRetryConfig,
    RepositoryIdentity,
    ReviewStatus,
    RunOutcome,
    RunRecord,
    TargetRepository,
    Workspace,
)
from opencode_tools.orchestrator import IssueOrchestrator, run_issue_pipeline
from opencode_tools.ports import AttemptLogSink, LogChannel
from opencode_tools.prompting import (
    build_architect_prompt,
    build_coder_prompt,
    build_reviewer_prompt,
)
from opencode_tools.state_machine import PipelineAction, PipelineState

_PHASE_BY_ROLE: dict[AgentRole, PipelinePhase] = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}

NOW = datetime(2026, 9, 15, 9, 0, 0, tzinfo=UTC)
RUN_ID = "20260915T090000.000000Z-abcdefabcdef"
MAX_REVIEW_CYCLES = 3
CONTROL_PLANE_DIGEST = "control-plane-digest-abc123"


class RecordingAttemptLogSink:
    """A minimal `AttemptLogSink` fake that records writes and closing."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.writes: list[tuple[LogChannel, bytes, datetime]] = []
        self.closed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        self.writes.append((channel, payload, timestamp))

    def close(self) -> None:
        self.closed = True


class SequencedRunStorePort:
    """A `RunStorePort` fake: one fresh sink per attempt, every `persist` OK."""

    def __init__(self) -> None:
        self.sink_calls: list[tuple[AgentRole, int | None, int]] = []
        self.opened_sinks: list[RecordingAttemptLogSink] = []
        self.persist_calls: list[RunRecord] = []

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        raise AssertionError("bootstrap concern, not exercised here")

    def open_attempt_sink(
        self, role: AgentRole, review_cycle: int | None, provider_attempt: int
    ) -> AttemptLogSink:
        self.sink_calls.append((role, review_cycle, provider_attempt))
        cycle_component = review_cycle if review_cycle is not None else 0
        sink = RecordingAttemptLogSink(
            path=Path(f"{role.value.lower()}-{cycle_component}-{provider_attempt}.log")
        )
        self.opened_sinks.append(sink)
        return sink

    def persist(self, record: RunRecord) -> PersistenceStatus:
        self.persist_calls.append(record)
        return PersistenceStatus.OK


class SequencedAgentRunner:
    """An `AgentRunner` fake returning one scripted result per call, in order."""

    def __init__(self, results: list[AgentResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[AgentRole, str, Workspace, int | None, int]] = []

    def run(
        self,
        role: AgentRole,
        prompt: str,
        workspace: Workspace,
        *,
        review_cycle: int | None,
        provider_attempt: int,
        sink: AttemptLogSink,
    ) -> AgentResult:
        self.calls.append((role, prompt, workspace, review_cycle, provider_attempt))
        return self._results.pop(0)


class SequencedGitSafetyPort:
    """A `GitSafetyPort` fake returning one scripted checkpoint per call."""

    def __init__(self, results: list[GitCheckRecord]) -> None:
        self._results = list(results)
        self.calls: list[
            tuple[TargetRepository, int, str, AgentRole | None, GitState | None]
        ] = []

    def check_runtime_location(self, runtime_root: Path) -> None:
        raise AssertionError("not exercised by this issue's orchestrator scope")

    def resolve_target(
        self, workspace: Workspace, target_root: Path
    ) -> TargetRepository:
        raise AssertionError("not exercised by this issue's orchestrator scope")

    def check(
        self,
        target: TargetRepository,
        *,
        sequence: int,
        purpose: str,
        role: AgentRole | None = None,
        baseline: GitState | None = None,
    ) -> GitCheckRecord:
        self.calls.append((target, sequence, purpose, role, baseline))
        return self._results.pop(0)


class RecordingOpenCodePreflightPort:
    """An `OpenCodePreflightPort` fake: `verify()` is never exercised here
    (M13-01's `bootstrap_run` concern); `recheck` always succeeds."""

    def __init__(self) -> None:
        self.recheck_calls: list[str] = []

    def verify(self) -> str:
        raise AssertionError("not exercised by this issue's orchestrator scope")

    def recheck(self, expected_digest: str) -> None:
        self.recheck_calls.append(expected_digest)


class SteppingClock:
    """A `Clock` fake whose `now()` advances by one second on every call."""

    def __init__(self, *, start: datetime = NOW) -> None:
        self._next = start

    def now(self) -> datetime:
        current = self._next
        self._next = current + timedelta(seconds=1)
        return current

    def monotonic_ns(self) -> int:
        raise AssertionError("not exercised by this issue's orchestrator scope")


class RecordingSleeper:
    """A `Sleeper` fake that records requested delays without ever sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


def _workspace_and_target(tmp_path: Path) -> tuple[Workspace, TargetRepository]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace = Workspace(root=workspace_root)
    target_root = workspace_root / "target"
    target_root.mkdir()
    target = TargetRepository(
        root=target_root,
        workspace_relative=Path("target"),
        git_common_dir=target_root / ".git",
    )
    return workspace, target


def _repository_identity() -> RepositoryIdentity:
    return RepositoryIdentity(
        host="github.com",
        owner="manueldellapa",
        repository="OpenCode-Tools",
        source="origin",
    )


def _issue_locator(*, issue_number: int = 49) -> IssueLocator:
    return IssueLocator(repository_identity=_repository_identity(), number=issue_number)


def _issue_ref(*, issue_number: int = 49) -> IssueRef:
    return IssueRef(
        locator=_issue_locator(issue_number=issue_number),
        url=f"https://github.com/manueldellapa/OpenCode-Tools/issues/{issue_number}",
        title="Comporre happy path architect -> coder -> reviewer",
    )


def _initial_record(
    *, workspace: Workspace, target: TargetRepository, git_baseline: GitState
) -> RunRecord:
    return RunRecord(
        schema_version=1,
        run_id=RUN_ID,
        artifact_path=workspace.root / ".opencode-tools" / "runs" / RUN_ID / "run.json",
        workspace=workspace,
        target=target,
        issue_number=49,
        config={},
        environment={},
        started_at=NOW,
        current_phase=PipelinePhase.ARCHITECT,
        persistence_status=PersistenceStatus.OK,
        issue_locator=_issue_locator(),
        git_baseline=git_baseline,
        git_checks=(),
        git_safety_status=GitSafetyStatus.SAFE,
    )


def _provider_retry_config() -> ProviderRetryConfig:
    return ProviderRetryConfig(
        max_attempts=3,
        initial_delay_seconds=1.0,
        multiplier=2.0,
        max_delay_seconds=30.0,
    )


def _git_probe_result(*, target_root: Path) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/git", "status"),
        cwd=target_root,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        duration_ns=1_000_000,
        return_code=0,
        timed_out=False,
        termination_confirmed=True,
        log_path=Path("git-probe.log"),
        stdout_byte_count=0,
        stdout_sha256="git-stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="git-stderr-digest",
        outcome=RunOutcome.SUCCEEDED,
    )


def _git_state(
    *,
    target_root: Path,
    fingerprint: str,
    staged: tuple[str, ...] = (),
    unstaged: tuple[str, ...] = (),
    untracked: tuple[str, ...] = (),
    branch: str | None = "main",
    head: str | None = "deadbeef",
) -> GitState:
    return GitState(
        root=target_root,
        branch=branch,
        head=head,
        porcelain_summary="",
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        fingerprint=fingerprint,
    )


def _git_check(
    *,
    target_root: Path,
    sequence: int,
    purpose: str,
    state: GitState,
    safety_status: GitSafetyStatus = GitSafetyStatus.SAFE,
) -> GitCheckRecord:
    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=(_git_probe_result(target_root=target_root),),
        state=state,
        safety_status=safety_status,
    )


def _agent_process_result(
    *, workspace_root: Path, outcome: RunOutcome = RunOutcome.SUCCEEDED
) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/opencode", "run"),
        cwd=workspace_root,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=5),
        duration_ns=5_000_000_000,
        return_code=0 if outcome is RunOutcome.SUCCEEDED else None,
        timed_out=False,
        termination_confirmed=True,
        log_path=Path("attempt.log"),
        stdout_byte_count=0,
        stdout_sha256="stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="stderr-digest",
        outcome=outcome,
    )


def _provider_diagnostic(*, retryable: bool = True) -> ProviderDiagnostic:
    return ProviderDiagnostic(
        source="opencode-stdout",
        signature="rate_limited",
        retryable=retryable,
        status_code=429,
    )


def _agent_result(
    *,
    role: AgentRole,
    review_cycle: int | None,
    workspace_root: Path,
    terminal_response: ParsedAgentResponse | None,
    provider_attempt: int = 1,
    process_outcome: RunOutcome = RunOutcome.SUCCEEDED,
    provider_diagnostic: ProviderDiagnostic | None = None,
) -> AgentResult:
    return AgentResult(
        role=role,
        phase=_PHASE_BY_ROLE[role],
        review_cycle=review_cycle,
        provider_attempt=provider_attempt,
        process=_agent_process_result(
            workspace_root=workspace_root, outcome=process_outcome
        ),
        terminal_response=terminal_response,
        session_id=(
            f"session-{role.value.lower()}-{review_cycle or 0}-{provider_attempt}"
        ),
        verified_agent=role.value.lower(),
        provider_diagnostic=provider_diagnostic,
        outcome=(
            RunOutcome.PROVIDER_ERROR
            if provider_diagnostic is not None
            else process_outcome
        ),
    )


def _orchestrator(
    *,
    workspace: Workspace,
    target: TargetRepository,
    agent_runner: SequencedAgentRunner,
    git_safety: SequencedGitSafetyPort,
    run_store: SequencedRunStorePort,
    git_baseline: GitState,
    sleeper: RecordingSleeper | None = None,
) -> IssueOrchestrator:
    return IssueOrchestrator(
        initial_record=_initial_record(
            workspace=workspace, target=target, git_baseline=git_baseline
        ),
        agent_runner=agent_runner,
        run_store=run_store,
        git_safety=git_safety,
        opencode_preflight=RecordingOpenCodePreflightPort(),
        control_plane_digest=CONTROL_PLANE_DIGEST,
        clock=SteppingClock(),
        sleeper=sleeper if sleeper is not None else RecordingSleeper(),
        provider_retry=_provider_retry_config(),
    )


def test_the_nominal_path_invokes_architect_coder_and_reviewer_in_canonical_order(
    tmp_path: Path,
) -> None:
    """The full happy path: architect READY, coder COMPLETED, reviewer
    APPROVED, with a non-trivial Git change inventory (staged, unstaged, and
    untracked paths alike) proving the reviewer receives the coder's *full*
    inventory, not just the newly untracked files."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="Understood the issue; the coder should add X and test Y.",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder_response = ParsedAgentResponse(
        role=AgentRole.CODER,
        body="Implemented X and added the Y test.",
        agent_status=AgentStatus.COMPLETED,
    )
    reviewer_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER,
        body="",
        review_status=ReviewStatus.APPROVED,
    )

    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=coder_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=reviewer_response,
            ),
        ]
    )

    coder_inventory_state = _git_state(
        target_root=target.root,
        fingerprint="fp-4",
        staged=("src/feature.py",),
        unstaged=("README.md",),
        untracked=("src/new_module.py", "tests/test_new_module.py"),
    )
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                state=coder_inventory_state,
            ),
            _git_check(
                target_root=target.root,
                sequence=4,
                purpose="REVIEWER:1:1:before",
                state=coder_inventory_state,
            ),
            _git_check(
                target_root=target.root,
                sequence=5,
                purpose="REVIEWER:1:1:after",
                state=coder_inventory_state,
            ),
        ]
    )
    run_store = SequencedRunStorePort()

    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    # Exactly three logical invocations, in the canonical order, each its
    # own primary-agent session (never a fourth agent).
    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
        AgentRole.REVIEWER,
    ]
    assert [call[3] for call in agent_runner.calls] == [None, 1, 1]
    assert [call[4] for call in agent_runner.calls] == [1, 1, 1]

    architect_prompt, coder_prompt, reviewer_prompt = (
        call[1] for call in agent_runner.calls
    )
    assert architect_prompt == build_architect_prompt(
        issue_locator=_issue_locator(),
        workspace_root=workspace.root,
        target_root=target.root,
    )
    # The architect's handoff and discovered IssueRef reach the coder intact.
    assert coder_prompt == build_coder_prompt(
        issue_ref=issue_ref,
        architect_handoff=architect_response.body,
        target_root=target.root,
        review_cycle=1,
        max_review_cycles=MAX_REVIEW_CYCLES,
        previous_review_feedback=None,
    )
    # The issue ref, handoff, coder report, full change inventory, and
    # canonical target all reach the reviewer intact -- nothing dropped.
    assert reviewer_prompt == build_reviewer_prompt(
        issue_ref=issue_ref,
        architect_handoff=architect_response.body,
        coder_report=coder_response.body,
        staged=coder_inventory_state.staged,
        unstaged=coder_inventory_state.unstaged,
        untracked=coder_inventory_state.untracked,
        test_scope="",
        target_root=target.root,
        review_cycle=1,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    # Each attempt persisted its own distinct AttemptRecord.
    assert len(run_store.persist_calls) == 3
    assert [len(record.attempts) for record in run_store.persist_calls] == [1, 2, 3]

    # APPROVED is translated into the postflight action, not an early
    # approval and not a FINAL_STATUS marker anywhere in this result.
    assert result.state == PipelineState(phase=PipelinePhase.POSTFLIGHT)
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is None
    assert len(result.architect) == 1
    assert result.coder_cycles == (result.coder_cycles[0],)
    assert len(result.coder_cycles[0]) == 1
    assert len(result.reviewer_cycles) == 1
    assert len(result.reviewer_cycles[0]) == 1


def test_coder_is_not_invoked_when_the_architect_reports_failed(
    tmp_path: Path,
) -> None:
    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="Could not access the issue.",
        agent_status=AgentStatus.FAILED,
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            )
        ]
    )
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [AgentRole.ARCHITECT]
    assert result.coder_cycles == ()
    assert result.reviewer_cycles == ()
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.AGENT_REPORTED_FAILURE


def test_coder_is_not_invoked_when_the_architect_envelope_is_malformed(
    tmp_path: Path,
) -> None:
    """A concrete adapter that could not parse the architect's response at
    all (e.g. a malformed/inconsistent `ISSUE_REF_JSON`) reports this as a
    technical `PROTOCOL_ERROR` with no `terminal_response` -- this pipeline
    must never invoke the coder on it, exactly as if READY had never been
    reported."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")

    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=None,
            )
        ]
    )
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [AgentRole.ARCHITECT]
    assert result.coder_cycles == ()
    assert result.reviewer_cycles == ()
    assert result.outcome is RunOutcome.PROTOCOL_ERROR


def test_reviewer_is_not_invoked_when_the_coder_reports_failed(tmp_path: Path) -> None:
    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder_response = ParsedAgentResponse(
        role=AgentRole.CODER,
        body="Could not implement the fix.",
        agent_status=AgentStatus.FAILED,
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=coder_response,
            ),
        ]
    )
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-1"),
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
    ]
    assert result.reviewer_cycles == ()
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.AGENT_REPORTED_FAILURE


def test_a_coder_partial_mutation_with_a_failed_report_never_reaches_the_reviewer(
    tmp_path: Path,
) -> None:
    """A coder that mutated the target (fingerprint delta, tolerated and
    `SAFE` for its own role) but ultimately reports `FAILED` must still
    never reach the reviewer -- and the mutation itself is never reverted or
    otherwise recovered (System Design FR-050/SS11.3)."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder_response = ParsedAgentResponse(
        role=AgentRole.CODER,
        body="Started the change but hit an unrecoverable error partway through.",
        agent_status=AgentStatus.FAILED,
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=coder_response,
            ),
        ]
    )
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                # A content mutation -- SAFE and tolerated for the coder --
                # is preserved as-is: this pipeline never resets it.
                state=_git_state(
                    target_root=target.root,
                    fingerprint="fp-1",
                    unstaged=("src/half_finished.py",),
                ),
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
    ]
    assert result.reviewer_cycles == ()
    assert result.outcome is RunOutcome.AGENT_REPORTED_FAILURE
    # The mutation is still visible on the persisted coder attempt -- never
    # discarded, never reverted.
    coder_attempt = result.coder_cycles[0][-1]
    assert coder_attempt.git_after is not None
    assert coder_attempt.git_after.state.unstaged == ("src/half_finished.py",)


def test_an_architect_git_mutation_blocks_the_coder_despite_a_ready_report(
    tmp_path: Path,
) -> None:
    """The architect is read-only: even a technically valid READY report
    must not open the door to the coder if the architect's own attempt left
    Git unsafe (System Design SS7.1/SS11.3) -- protocol precedence and Git
    safety are orthogonal, and this pipeline must combine them itself."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            )
        ]
    )
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-1"),
                safety_status=GitSafetyStatus.UNSAFE,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [AgentRole.ARCHITECT]
    assert result.coder_cycles == ()
    assert result.reviewer_cycles == ()
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.GIT_SAFETY_ERROR


def test_a_reviewer_git_mutation_reports_a_git_safety_error_despite_approval(
    tmp_path: Path,
) -> None:
    """A reviewer `APPROVED` that nonetheless left Git unsafe must not be
    reported as an ordinary approval-to-postflight advance: the Git safety
    error, not the reviewer's own claim, is what this pipeline reports."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder_response = ParsedAgentResponse(
        role=AgentRole.CODER,
        body="done",
        agent_status=AgentStatus.COMPLETED,
    )
    reviewer_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER,
        body="",
        review_status=ReviewStatus.APPROVED,
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=coder_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=reviewer_response,
            ),
        ]
    )
    coder_after_state = _git_state(target_root=target.root, fingerprint="fp-2")
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                state=coder_after_state,
            ),
            _git_check(
                target_root=target.root,
                sequence=4,
                purpose="REVIEWER:1:1:before",
                state=coder_after_state,
            ),
            _git_check(
                target_root=target.root,
                sequence=5,
                purpose="REVIEWER:1:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-3"),
                safety_status=GitSafetyStatus.UNSAFE,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
        AgentRole.REVIEWER,
    ]
    assert len(result.reviewer_cycles) == 1
    assert len(result.reviewer_cycles[0]) == 1
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.GIT_SAFETY_ERROR


def test_a_rework_cycle_carries_full_feedback_to_the_next_coder_then_approves(
    tmp_path: Path,
) -> None:
    """`CHANGES_REQUIRED` below `max_review_cycles` composes the next
    coder(2)/reviewer(2) cycle in the same loop, carrying the reviewer's own
    body into the next coder's prompt verbatim as `previous_review_feedback`
    (M13-03)."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="Understood the issue; add X.",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder1_response = ParsedAgentResponse(
        role=AgentRole.CODER, body="Added X.", agent_status=AgentStatus.COMPLETED
    )
    reviewer1_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER,
        body="X is missing an edge-case handler for empty input.",
        review_status=ReviewStatus.CHANGES_REQUIRED,
    )
    coder2_response = ParsedAgentResponse(
        role=AgentRole.CODER,
        body="Handled the empty-input edge case.",
        agent_status=AgentStatus.COMPLETED,
    )
    reviewer2_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER, body="", review_status=ReviewStatus.APPROVED
    )

    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=coder1_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=reviewer1_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=2,
                workspace_root=workspace.root,
                terminal_response=coder2_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=2,
                workspace_root=workspace.root,
                terminal_response=reviewer2_response,
            ),
        ]
    )
    coder1_after = _git_state(target_root=target.root, fingerprint="fp-2")
    coder2_after = _git_state(target_root=target.root, fingerprint="fp-4")
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                state=coder1_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=4,
                purpose="REVIEWER:1:1:before",
                state=coder1_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=5,
                purpose="REVIEWER:1:1:after",
                state=coder1_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=6,
                purpose="CODER:2:1:before",
                state=coder1_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=7,
                purpose="CODER:2:1:after",
                state=coder2_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=8,
                purpose="REVIEWER:2:1:before",
                state=coder2_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=9,
                purpose="REVIEWER:2:1:after",
                state=coder2_after,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
        AgentRole.REVIEWER,
        AgentRole.CODER,
        AgentRole.REVIEWER,
    ]
    assert [call[3] for call in agent_runner.calls] == [None, 1, 1, 2, 2]

    coder2_prompt = agent_runner.calls[3][1]
    assert coder2_prompt == build_coder_prompt(
        issue_ref=issue_ref,
        architect_handoff=architect_response.body,
        target_root=target.root,
        review_cycle=2,
        max_review_cycles=MAX_REVIEW_CYCLES,
        previous_review_feedback=reviewer1_response.body,
    )
    reviewer2_prompt = agent_runner.calls[4][1]
    assert reviewer2_prompt == build_reviewer_prompt(
        issue_ref=issue_ref,
        architect_handoff=architect_response.body,
        coder_report=coder2_response.body,
        staged=coder2_after.staged,
        unstaged=coder2_after.unstaged,
        untracked=coder2_after.untracked,
        test_scope="",
        target_root=target.root,
        review_cycle=2,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert len(run_store.persist_calls) == 5
    assert len(result.coder_cycles) == 2
    assert len(result.reviewer_cycles) == 2
    assert result.state == PipelineState(phase=PipelinePhase.POSTFLIGHT)
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is None


def test_changes_required_at_the_only_cycle_exhausts_review_without_a_further_coder(
    tmp_path: Path,
) -> None:
    """With `max_review_cycles=1`, a single `CHANGES_REQUIRED` is already at
    the last allowed cycle: it must resolve to `REVIEW_CYCLES_EXHAUSTED`
    without ever invoking a second coder."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder_response = ParsedAgentResponse(
        role=AgentRole.CODER, body="done", agent_status=AgentStatus.COMPLETED
    )
    reviewer_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER,
        body="Not quite right yet.",
        review_status=ReviewStatus.CHANGES_REQUIRED,
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=coder_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=reviewer_response,
            ),
        ]
    )
    coder_after = _git_state(target_root=target.root, fingerprint="fp-2")
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                state=coder_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=4,
                purpose="REVIEWER:1:1:before",
                state=coder_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=5,
                purpose="REVIEWER:1:1:after",
                state=coder_after,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=1,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
        AgentRole.REVIEWER,
    ]
    assert len(result.coder_cycles) == 1
    assert len(result.reviewer_cycles) == 1
    assert result.state == PipelineState(phase=PipelinePhase.POSTFLIGHT)
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.REVIEW_CYCLES_EXHAUSTED


def test_changes_required_persists_through_every_cycle_until_exhaustion(
    tmp_path: Path,
) -> None:
    """With `max_review_cycles=2`, a `CHANGES_REQUIRED` at cycle 1 opens
    cycle 2, but a further `CHANGES_REQUIRED` at cycle 2 (the last allowed
    one) exhausts review without ever invoking a third coder."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder1_response = ParsedAgentResponse(
        role=AgentRole.CODER, body="attempt 1", agent_status=AgentStatus.COMPLETED
    )
    reviewer1_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER,
        body="Still missing a case.",
        review_status=ReviewStatus.CHANGES_REQUIRED,
    )
    coder2_response = ParsedAgentResponse(
        role=AgentRole.CODER, body="attempt 2", agent_status=AgentStatus.COMPLETED
    )
    reviewer2_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER,
        body="Still not right.",
        review_status=ReviewStatus.CHANGES_REQUIRED,
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=coder1_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=reviewer1_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=2,
                workspace_root=workspace.root,
                terminal_response=coder2_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=2,
                workspace_root=workspace.root,
                terminal_response=reviewer2_response,
            ),
        ]
    )
    coder1_after = _git_state(target_root=target.root, fingerprint="fp-2")
    coder2_after = _git_state(target_root=target.root, fingerprint="fp-4")
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                state=coder1_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=4,
                purpose="REVIEWER:1:1:before",
                state=coder1_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=5,
                purpose="REVIEWER:1:1:after",
                state=coder1_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=6,
                purpose="CODER:2:1:before",
                state=coder1_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=7,
                purpose="CODER:2:1:after",
                state=coder2_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=8,
                purpose="REVIEWER:2:1:before",
                state=coder2_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=9,
                purpose="REVIEWER:2:1:after",
                state=coder2_after,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=2,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
        AgentRole.REVIEWER,
        AgentRole.CODER,
        AgentRole.REVIEWER,
    ]
    assert [call[3] for call in agent_runner.calls] == [None, 1, 1, 2, 2]
    assert len(result.coder_cycles) == 2
    assert len(result.reviewer_cycles) == 2
    assert result.state == PipelineState(phase=PipelinePhase.POSTFLIGHT)
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.REVIEW_CYCLES_EXHAUSTED


def test_a_coder_provider_error_recovers_on_retry_without_extra_role_invocations(
    tmp_path: Path,
) -> None:
    """A trusted, retryable provider error on the coder's first attempt
    recovers on the second, still within review cycle 1 -- the provider
    retry never advances `review_cycle` and never repeats architect or
    reviewer (System Design SS12.1-SS12.2)."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder_response = ParsedAgentResponse(
        role=AgentRole.CODER, body="done", agent_status=AgentStatus.COMPLETED
    )
    reviewer_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER, body="", review_status=ReviewStatus.APPROVED
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                workspace_root=workspace.root,
                terminal_response=None,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=2,
                workspace_root=workspace.root,
                terminal_response=coder_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=reviewer_response,
            ),
        ]
    )
    same_fingerprint = _git_state(target_root=target.root, fingerprint="fp-0")
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=4,
                purpose="CODER:1:2:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=5,
                purpose="CODER:1:2:after",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=6,
                purpose="REVIEWER:1:1:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=7,
                purpose="REVIEWER:1:1:after",
                state=same_fingerprint,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    sleeper = RecordingSleeper()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
        sleeper=sleeper,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
        AgentRole.CODER,
        AgentRole.REVIEWER,
    ]
    assert [call[3] for call in agent_runner.calls] == [None, 1, 1, 1]
    assert [call[4] for call in agent_runner.calls] == [1, 1, 2, 1]
    assert sleeper.calls == [1.0]
    assert len(result.coder_cycles) == 1
    assert len(result.coder_cycles[0]) == 2
    assert len(result.reviewer_cycles) == 1
    assert result.outcome is None
    assert result.action is PipelineAction.ENTER_POSTFLIGHT


def test_a_coder_provider_error_exhausts_the_budget_without_reaching_the_reviewer(
    tmp_path: Path,
) -> None:
    """A trusted, retryable provider error that never recovers exhausts
    exactly the configured attempt budget, sleeping the exact capped backoff
    between attempts, and never reaches the reviewer."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    same_fingerprint = _git_state(target_root=target.root, fingerprint="fp-0")
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                workspace_root=workspace.root,
                terminal_response=None,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=2,
                workspace_root=workspace.root,
                terminal_response=None,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=3,
                workspace_root=workspace.root,
                terminal_response=None,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
        ]
    )
    git_checks = [
        _git_check(
            target_root=target.root,
            sequence=0,
            purpose="ARCHITECT:0:1:before",
            state=same_fingerprint,
        ),
        _git_check(
            target_root=target.root,
            sequence=1,
            purpose="ARCHITECT:0:1:after",
            state=same_fingerprint,
        ),
    ]
    for attempt in (1, 2, 3):
        git_checks.append(
            _git_check(
                target_root=target.root,
                sequence=len(git_checks),
                purpose=f"CODER:1:{attempt}:before",
                state=same_fingerprint,
            )
        )
        git_checks.append(
            _git_check(
                target_root=target.root,
                sequence=len(git_checks),
                purpose=f"CODER:1:{attempt}:after",
                state=same_fingerprint,
            )
        )
    git_safety = SequencedGitSafetyPort(git_checks)
    run_store = SequencedRunStorePort()
    sleeper = RecordingSleeper()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
        sleeper=sleeper,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
        AgentRole.CODER,
        AgentRole.CODER,
    ]
    assert [call[4] for call in agent_runner.calls] == [1, 1, 2, 3]
    assert sleeper.calls == [1.0, 2.0]  # capped exponential backoff, no jitter
    assert result.reviewer_cycles == ()
    assert len(result.coder_cycles) == 1
    assert len(result.coder_cycles[0]) == 3
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.PROVIDER_ERROR


def test_a_coder_provider_error_with_a_target_delta_suppresses_retry(
    tmp_path: Path,
) -> None:
    """A coder's own provider error is never retried once its target
    fingerprint already changed -- the outcome stays `PROVIDER_ERROR`, the
    suppression is recorded, and the mutation itself is left untouched
    (System Design SS12.2, AC-013)."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                workspace_root=workspace.root,
                terminal_response=None,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
        ]
    )
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                # SAFE and tolerated for the coder, but the fingerprint
                # changed -- a delta the retry policy must respect.
                state=_git_state(
                    target_root=target.root,
                    fingerprint="fp-1",
                    unstaged=("src/partial.py",),
                ),
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    sleeper = RecordingSleeper()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
        sleeper=sleeper,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
    ]
    assert sleeper.calls == []  # never retried, so never slept
    assert result.reviewer_cycles == ()
    assert len(result.coder_cycles) == 1
    assert len(result.coder_cycles[0]) == 1
    coder_attempt = result.coder_cycles[0][0]
    assert coder_attempt.retry_decision is not None
    assert coder_attempt.retry_decision.should_retry is False
    assert coder_attempt.retry_decision.retry_suppressed_due_to_target_change is True
    assert result.outcome is RunOutcome.PROVIDER_ERROR
    # The mutation itself is left exactly as the coder made it.
    assert coder_attempt.git_after is not None
    assert coder_attempt.git_after.state.unstaged == ("src/partial.py",)


def test_an_untrusted_provider_diagnostic_is_never_retried(tmp_path: Path) -> None:
    """A provider-shaped signal that the classifier itself marks untrusted
    (`retryable=False`) is never retried, even with attempt budget left and
    an unchanged target -- a "lookalike" provider error is not authorized
    for a technical retry (System Design SS12.2)."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    same_fingerprint = _git_state(target_root=target.root, fingerprint="fp-0")
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                provider_attempt=1,
                workspace_root=workspace.root,
                terminal_response=None,
                provider_diagnostic=_provider_diagnostic(retryable=False),
            ),
        ]
    )
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                state=same_fingerprint,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    sleeper = RecordingSleeper()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
        sleeper=sleeper,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
    ]
    assert sleeper.calls == []
    assert len(result.coder_cycles[0]) == 1
    assert result.reviewer_cycles == ()
    assert result.outcome is RunOutcome.PROVIDER_ERROR


def test_a_reviewer_provider_retry_recovers_without_invoking_the_coder_again(
    tmp_path: Path,
) -> None:
    """A reviewer's own provider-error retry stays within the reviewer's
    logical invocation: it must never re-invoke the coder (System Design
    SS12.2's "un retry reviewer non richiama il coder")."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder_response = ParsedAgentResponse(
        role=AgentRole.CODER, body="done", agent_status=AgentStatus.COMPLETED
    )
    reviewer_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER, body="", review_status=ReviewStatus.APPROVED
    )
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=coder_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=1,
                provider_attempt=1,
                workspace_root=workspace.root,
                terminal_response=None,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=1,
                provider_attempt=2,
                workspace_root=workspace.root,
                terminal_response=reviewer_response,
            ),
        ]
    )
    coder_after = _git_state(target_root=target.root, fingerprint="fp-2")
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="CODER:1:1:before",
                state=_git_state(target_root=target.root, fingerprint="fp-0"),
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="CODER:1:1:after",
                state=coder_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=4,
                purpose="REVIEWER:1:1:before",
                state=coder_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=5,
                purpose="REVIEWER:1:1:after",
                state=coder_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=6,
                purpose="REVIEWER:1:2:before",
                state=coder_after,
            ),
            _git_check(
                target_root=target.root,
                sequence=7,
                purpose="REVIEWER:1:2:after",
                state=coder_after,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    sleeper = RecordingSleeper()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
        sleeper=sleeper,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
        AgentRole.REVIEWER,
        AgentRole.REVIEWER,
    ]
    assert [call[4] for call in agent_runner.calls] == [1, 1, 1, 2]
    assert sleeper.calls == [1.0]
    assert len(result.coder_cycles) == 1  # the coder was never invoked twice
    assert len(result.reviewer_cycles[0]) == 2
    assert result.outcome is None
    assert result.action is PipelineAction.ENTER_POSTFLIGHT


def test_an_architect_provider_error_recovers_on_retry_before_the_coder_runs(
    tmp_path: Path,
) -> None:
    """Provider-error recovery works identically for the architect: the
    retry stays within the architect's own logical invocation, and the
    coder is only ever invoked once the architect's *last* attempt is
    `READY`."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")
    issue_ref = _issue_ref()

    architect_response = ParsedAgentResponse(
        role=AgentRole.ARCHITECT,
        body="plan",
        agent_status=AgentStatus.READY,
        issue_ref=issue_ref,
    )
    coder_response = ParsedAgentResponse(
        role=AgentRole.CODER, body="done", agent_status=AgentStatus.COMPLETED
    )
    reviewer_response = ParsedAgentResponse(
        role=AgentRole.REVIEWER, body="", review_status=ReviewStatus.APPROVED
    )
    same_fingerprint = _git_state(target_root=target.root, fingerprint="fp-0")
    agent_runner = SequencedAgentRunner(
        [
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                provider_attempt=1,
                workspace_root=workspace.root,
                terminal_response=None,
                provider_diagnostic=_provider_diagnostic(retryable=True),
            ),
            _agent_result(
                role=AgentRole.ARCHITECT,
                review_cycle=None,
                provider_attempt=2,
                workspace_root=workspace.root,
                terminal_response=architect_response,
            ),
            _agent_result(
                role=AgentRole.CODER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=coder_response,
            ),
            _agent_result(
                role=AgentRole.REVIEWER,
                review_cycle=1,
                workspace_root=workspace.root,
                terminal_response=reviewer_response,
            ),
        ]
    )
    git_safety = SequencedGitSafetyPort(
        [
            _git_check(
                target_root=target.root,
                sequence=0,
                purpose="ARCHITECT:0:1:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=1,
                purpose="ARCHITECT:0:1:after",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=2,
                purpose="ARCHITECT:0:2:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=3,
                purpose="ARCHITECT:0:2:after",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=4,
                purpose="CODER:1:1:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=5,
                purpose="CODER:1:1:after",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=6,
                purpose="REVIEWER:1:1:before",
                state=same_fingerprint,
            ),
            _git_check(
                target_root=target.root,
                sequence=7,
                purpose="REVIEWER:1:1:after",
                state=same_fingerprint,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    sleeper = RecordingSleeper()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
        sleeper=sleeper,
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.ARCHITECT,
        AgentRole.CODER,
        AgentRole.REVIEWER,
    ]
    assert [call[4] for call in agent_runner.calls] == [1, 2, 1, 1]
    assert sleeper.calls == [1.0]
    assert len(result.architect) == 2
    assert len(result.coder_cycles) == 1
    assert result.outcome is None
    assert result.action is PipelineAction.ENTER_POSTFLIGHT

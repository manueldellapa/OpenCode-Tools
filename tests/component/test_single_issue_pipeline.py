"""Component tests for `orchestrator.run_issue_pipeline` (M13-02).

Drives the nominal `architect -> coder(1) -> reviewer(1)` happy path against
an `IssueOrchestrator` built directly from deterministic port fakes -- a
`SequencedAgentRunner`, `SequencedGitSafetyPort`, a recording `RunStorePort`,
and an `OpenCodePreflightPort` fake whose `recheck` always succeeds -- using
real `tmp_path`-derived absolute paths for `Workspace`/`TargetRepository`,
exactly as a composed run would see them. No concrete adapter, no
subprocess, no real GitHub or OpenCode. Proves the canonical invocation
order and its preconditions (architect `READY` with a locator-consistent
`ISSUE_REF_JSON` before the coder; coder `COMPLETED` before the reviewer),
that the architect's handoff/issue-ref, the coder's report, and its own
final Git change inventory reach the next role's prompt byte-for-byte
(never summarized or judged), that a read-only role's own Git mutation
blocks the next phase exactly as a protocol failure would, and that a
reviewer `APPROVED`/`CHANGES_REQUIRED` verdict is only ever *reported* as
`state_machine.transition`'s own next action -- never acted upon, never an
early approval, never rendered as CLI output (that remains M13-03/M13-04).
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


def _agent_result(
    *,
    role: AgentRole,
    review_cycle: int | None,
    workspace_root: Path,
    terminal_response: ParsedAgentResponse | None,
    process_outcome: RunOutcome = RunOutcome.SUCCEEDED,
) -> AgentResult:
    return AgentResult(
        role=role,
        phase=_PHASE_BY_ROLE[role],
        review_cycle=review_cycle,
        provider_attempt=1,
        process=_agent_process_result(
            workspace_root=workspace_root, outcome=process_outcome
        ),
        terminal_response=terminal_response,
        session_id=f"session-{role.value.lower()}-{review_cycle or 0}-1",
        verified_agent=role.value.lower(),
        provider_diagnostic=None,
        outcome=process_outcome,
    )


def _orchestrator(
    *,
    workspace: Workspace,
    target: TargetRepository,
    agent_runner: SequencedAgentRunner,
    git_safety: SequencedGitSafetyPort,
    run_store: SequencedRunStorePort,
    git_baseline: GitState,
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
        sleeper=RecordingSleeper(),
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
    assert len(result.coder) == 1
    assert len(result.reviewer) == 1


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
    assert result.coder == ()
    assert result.reviewer == ()
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
    assert result.coder == ()
    assert result.reviewer == ()
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
    assert result.reviewer == ()
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.AGENT_REPORTED_FAILURE


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
    assert result.coder == ()
    assert result.reviewer == ()
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
    assert len(result.reviewer) == 1
    assert result.action is PipelineAction.ENTER_POSTFLIGHT
    assert result.outcome is RunOutcome.GIT_SAFETY_ERROR


def test_a_reviewer_changes_required_is_reported_without_invoking_a_second_coder(
    tmp_path: Path,
) -> None:
    """`CHANGES_REQUIRED` opens a rework cycle in the state machine's own
    verdict (`INVOKE_CODER`, `review_cycle == 2`), but composing that second
    coder invocation is M13-03's job, not this pipeline's -- it must only
    ever report the action, never act on it."""

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
        body="Please also handle the edge case.",
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
                state=coder_after_state,
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
    assert result.action is PipelineAction.INVOKE_CODER
    assert result.state == PipelineState(phase=PipelinePhase.CODER, review_cycle=2)
    assert result.outcome is None
    assert len(result.reviewer) == 1

"""Component tests for `orchestrator.run_issue_pipeline` and
`orchestrator.finalize_run` (M13-02, M13-03, M13-04).

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
fourth agent or acting on a reviewer's verdict as a final status.

The `finalize_run` tests pick up exactly where `run_issue_pipeline` stops --
using its own `IssuePipelineResult` and `IssueOrchestrator.record` to derive
`finalize_run`'s inputs, exactly as a future composition root would -- and
prove the M13-04 convergence: a reviewer's historical `APPROVED` never
survives a later Git drift, cycle exhaustion/provider exhaustion/
interruption all finalize `FAILED` with the matching exit code, an
unconfirmed termination forces an `INDETERMINATE` postflight and quarantines
the target before the lease is ever released, a quarantine failure stays
visible without masking the original cause, and a failure of `finalize_run`'s
own last write falls back to the last snapshot already known durable rather
than ever reporting an unpersisted `APPROVED`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    FinalStatus,
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
from opencode_tools.errors import LoggingError
from opencode_tools.orchestrator import (
    IssueOrchestrator,
    IssuePipelineResult,
    LogicalInvocationResult,
    finalize_run,
    run_issue_pipeline,
)
from opencode_tools.ports import AttemptLogSink, GitSafetyPort, LogChannel
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
    """A `RunStorePort` fake: one fresh sink per attempt. `persist` returns
    `OK` unless `persist_results` scripts otherwise for that call; `order_log`,
    when given, records a tag per call so a test can assert this port's
    calls interleave correctly with every other port's own."""

    def __init__(
        self,
        *,
        persist_results: list[PersistenceStatus] | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        self.sink_calls: list[tuple[AgentRole, int | None, int]] = []
        self.opened_sinks: list[RecordingAttemptLogSink] = []
        self.persist_calls: list[RunRecord] = []
        self._persist_results = (
            list(persist_results) if persist_results is not None else None
        )
        self._order_log = order_log

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
        if self._order_log is not None:
            self._order_log.append(f"persist:{record.current_phase.value}")
        self.persist_calls.append(record)
        if self._persist_results is not None:
            return self._persist_results.pop(0)
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
    """A `GitSafetyPort` fake returning one scripted checkpoint per call.
    `order_log`, when given, records a tag per call (see `SequencedRunStorePort`)."""

    def __init__(
        self, results: list[GitCheckRecord], *, order_log: list[str] | None = None
    ) -> None:
        self._results = list(results)
        self.calls: list[
            tuple[TargetRepository, int, str, AgentRole | None, GitState | None]
        ] = []
        self._order_log = order_log

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
        if self._order_log is not None:
            self._order_log.append(f"git_check:{purpose}")
        self.calls.append((target, sequence, purpose, role, baseline))
        return self._results.pop(0)


class ComparingGitSafetyPort:
    """A `GitSafetyPort` fake that computes each checkpoint's `safety_status`
    the same way the real `check_git_state` does -- branch/HEAD drift is
    `UNSAFE` regardless of `role`; a fingerprint delta is `SAFE` only for
    `AgentRole.CODER` -- from a scripted sequence of fresh captures.

    Unlike `SequencedGitSafetyPort`, which hands back a pre-baked verdict no
    matter what `baseline`/`role` it is given, this fake actually reacts to
    them: it is the only way to prove `finalize_run`'s postflight probe
    compares against the *correct* baseline with the *correct* role, rather
    than merely forwarding whatever a scripted fake happens to return.
    """

    def __init__(self, states: list[GitState]) -> None:
        self._states = list(states)
        self.calls: list[tuple[int, str, AgentRole | None, GitState | None]] = []

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
        self.calls.append((sequence, purpose, role, baseline))
        state = self._states.pop(0)
        if baseline is None:
            safety_status = GitSafetyStatus.SAFE
        else:
            drifted = state.branch != baseline.branch or state.head != baseline.head
            fingerprint_delta_unsafe = (
                state.fingerprint != baseline.fingerprint
                and role is not AgentRole.CODER
            )
            safety_status = (
                GitSafetyStatus.UNSAFE
                if drifted or fingerprint_delta_unsafe
                else GitSafetyStatus.SAFE
            )
        return GitCheckRecord(
            sequence=sequence,
            purpose=purpose,
            process_results=(),
            state=state,
            safety_status=safety_status,
            compared_to=baseline.fingerprint if baseline is not None else None,
        )


class RecordingOpenCodePreflightPort:
    """An `OpenCodePreflightPort` fake: `verify()` is never exercised here
    (M13-01's `bootstrap_run` concern); `recheck` always succeeds."""

    def __init__(self) -> None:
        self.recheck_calls: list[str] = []

    def verify(self) -> str:
        raise AssertionError("not exercised by this issue's orchestrator scope")

    def recheck(self, expected_digest: str) -> None:
        self.recheck_calls.append(expected_digest)


class RecordingTargetLease:
    """A `TargetLease` fake: records release/quarantine without OS locking.
    `quarantine_error`, when given, is raised by `quarantine` after still
    recording the attempt (`LoggingError`, never swallowed by this fake --
    the caller decides what to do with it). `order_log`, when given, records
    a tag per call (see `SequencedRunStorePort`)."""

    def __init__(
        self,
        *,
        quarantine_error: LoggingError | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        self.released = False
        self.quarantine_reasons: list[str] = []
        self._quarantine_error = quarantine_error
        self._order_log = order_log

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._order_log is not None:
            self._order_log.append("lease_release")
        self.released = True

    def quarantine(self, reason: str) -> None:
        if self._order_log is not None:
            self._order_log.append("lease_quarantine")
        self.quarantine_reasons.append(reason)
        if self._quarantine_error is not None:
            raise self._quarantine_error


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
    git_safety: GitSafetyPort,
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


def _trigger_outcome(result: IssuePipelineResult) -> RunOutcome:
    """Derive `finalize_run`'s own `trigger_outcome` input the way a future
    composition root would: `IssuePipelineResult.outcome` whenever the
    pipeline itself named one, or `SUCCEEDED` for an ordinary reviewer
    `APPROVED` advance (the only case it leaves `None`)."""

    return result.outcome if result.outcome is not None else RunOutcome.SUCCEEDED


def _last_review_status(result: IssuePipelineResult) -> ReviewStatus | None:
    """The reviewer's own last, historical verdict -- never re-derived
    through `_attempt_result`'s Git-safety gating, which is a different
    question (whether the *next* phase may run), not what the reviewer
    itself claimed."""

    if not result.reviewer_cycles:
        return None
    last_attempt = result.reviewer_cycles[-1][-1]
    if (
        last_attempt.agent_result is None
        or last_attempt.agent_result.terminal_response is None
    ):
        return None
    return last_attempt.agent_result.terminal_response.review_status


def _all_attempts(result: IssuePipelineResult) -> tuple[LogicalInvocationResult, ...]:
    return (
        *result.architect,
        *(attempt for cycle in result.coder_cycles for attempt in cycle),
        *(attempt for cycle in result.reviewer_cycles for attempt in cycle),
    )


def _termination_confirmed(result: IssuePipelineResult) -> bool | None:
    """Whether every attempt this pipeline ran had its own process group's
    termination confirmed -- `None` if no attempt with a technical
    `AgentResult` ever ran (never the case here: the architect always
    does), `False` if even one did not."""

    confirmed_values = [
        attempt.agent_result.process.termination_confirmed
        for attempt in _all_attempts(result)
        if attempt.agent_result is not None
    ]
    if not confirmed_values:
        return None
    return all(value is True for value in confirmed_values)


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


def _happy_path_responses(
    issue_ref: IssueRef,
) -> tuple[ParsedAgentResponse, ParsedAgentResponse, ParsedAgentResponse]:
    return (
        ParsedAgentResponse(
            role=AgentRole.ARCHITECT,
            body="plan",
            agent_status=AgentStatus.READY,
            issue_ref=issue_ref,
        ),
        ParsedAgentResponse(
            role=AgentRole.CODER, body="done", agent_status=AgentStatus.COMPLETED
        ),
        ParsedAgentResponse(
            role=AgentRole.REVIEWER, body="", review_status=ReviewStatus.APPROVED
        ),
    )


def _happy_path_git_checks(target_root: Path) -> list[GitCheckRecord]:
    before_coder = _git_state(target_root=target_root, fingerprint="fp-0")
    after_coder = _git_state(target_root=target_root, fingerprint="fp-2")
    return [
        _git_check(
            target_root=target_root,
            sequence=0,
            purpose="ARCHITECT:0:1:before",
            state=before_coder,
        ),
        _git_check(
            target_root=target_root,
            sequence=1,
            purpose="ARCHITECT:0:1:after",
            state=before_coder,
        ),
        _git_check(
            target_root=target_root,
            sequence=2,
            purpose="CODER:1:1:before",
            state=before_coder,
        ),
        _git_check(
            target_root=target_root,
            sequence=3,
            purpose="CODER:1:1:after",
            state=after_coder,
        ),
        _git_check(
            target_root=target_root,
            sequence=4,
            purpose="REVIEWER:1:1:before",
            state=after_coder,
        ),
        _git_check(
            target_root=target_root,
            sequence=5,
            purpose="REVIEWER:1:1:after",
            state=after_coder,
        ),
    ]


def _run_happy_path(
    *,
    workspace: Workspace,
    target: TargetRepository,
    git_baseline: GitState,
    git_safety: GitSafetyPort,
    run_store: SequencedRunStorePort,
) -> tuple[IssueOrchestrator, IssuePipelineResult]:
    issue_ref = _issue_ref()
    architect_response, coder_response, reviewer_response = _happy_path_responses(
        issue_ref
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
    return orchestrator, result


def test_finalize_run_approves_after_a_clean_reviewer_approval_in_the_correct_order(
    tmp_path: Path,
) -> None:
    """The canonical success path: a `SAFE` postflight continuity check
    against the *last checkpoint the pipeline itself accepted*, an unchanged
    branch/HEAD since the run's original baseline, and `OK` persistence
    together with the reviewer's own historical `APPROVED` produce
    `FinalStatus.APPROVED` and exit code 0 (System Design SS8.4) -- and the
    postflight probe, the final persist, and the lease release happen
    strictly after the pipeline itself finished, and in that order (M13-04)."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")

    order_log: list[str] = []
    checks = _happy_path_git_checks(target.root)
    checks.append(
        _git_check(
            target_root=target.root,
            sequence=6,
            purpose="postflight",
            state=_git_state(target_root=target.root, fingerprint="fp-9"),
        )
    )
    git_safety = SequencedGitSafetyPort(checks, order_log=order_log)
    run_store = SequencedRunStorePort(order_log=order_log)
    lease = RecordingTargetLease(order_log=order_log)

    orchestrator, result = _run_happy_path(
        workspace=workspace,
        target=target,
        git_baseline=git_baseline,
        git_safety=git_safety,
        run_store=run_store,
    )

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=False,
        termination_confirmed=_termination_confirmed(result),
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert _last_review_status(result) is ReviewStatus.APPROVED
    assert issue_result.final_status is FinalStatus.APPROVED
    assert issue_result.expected_exit_code == 0
    assert issue_result.trigger_outcome is RunOutcome.SUCCEEDED
    assert issue_result.persistence_status is PersistenceStatus.OK
    assert issue_result.changes_preserved is True

    final_record = run_store.persist_calls[-1]
    assert final_record.current_phase is PipelinePhase.FINISHED
    assert final_record.final_status is FinalStatus.APPROVED
    assert final_record.git_postflight == checks[-1]

    # The postflight probe, the final persist, and the lease release happen
    # strictly after every pipeline-owned call, and in exactly that order.
    assert order_log[-3:] == [
        "git_check:postflight",
        "persist:FINISHED",
        "lease_release",
    ]
    assert lease.released is True


def test_finalize_run_denies_approval_when_postflight_drifts_after_a_historical_approval(
    tmp_path: Path,
) -> None:
    """System Design SS8.4: the reviewer's own `APPROVED` "resta un dato
    storico e non viene riscritto" -- but a branch drift discovered only at
    the final postflight still denies approval; the historical verdict is
    reported to the gate unchanged, the gate is what says no."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")

    checks = _happy_path_git_checks(target.root)
    checks.append(
        _git_check(
            target_root=target.root,
            sequence=6,
            purpose="postflight",
            state=_git_state(
                target_root=target.root, fingerprint="fp-9", branch="other-branch"
            ),
            safety_status=GitSafetyStatus.UNSAFE,
        )
    )
    git_safety = SequencedGitSafetyPort(checks)
    run_store = SequencedRunStorePort()
    lease = RecordingTargetLease()

    orchestrator, result = _run_happy_path(
        workspace=workspace,
        target=target,
        git_baseline=git_baseline,
        git_safety=git_safety,
        run_store=run_store,
    )

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=False,
        termination_confirmed=_termination_confirmed(result),
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert _last_review_status(result) is ReviewStatus.APPROVED  # unchanged, historical
    assert issue_result.final_status is FinalStatus.FAILED
    assert issue_result.trigger_outcome is RunOutcome.GIT_SAFETY_ERROR
    assert issue_result.expected_exit_code == 30
    assert lease.released is True


def test_finalize_run_reports_review_cycles_exhausted_as_failed(tmp_path: Path) -> None:
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
    checks = _happy_path_git_checks(target.root)
    checks.append(
        _git_check(
            target_root=target.root,
            sequence=6,
            purpose="postflight",
            state=_git_state(target_root=target.root, fingerprint="fp-9"),
        )
    )
    git_safety = SequencedGitSafetyPort(checks)
    run_store = SequencedRunStorePort()
    lease = RecordingTargetLease()
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
    assert result.outcome is RunOutcome.REVIEW_CYCLES_EXHAUSTED

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=False,
        termination_confirmed=_termination_confirmed(result),
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=1,
    )

    assert _last_review_status(result) is ReviewStatus.CHANGES_REQUIRED
    assert issue_result.final_status is FinalStatus.FAILED
    assert issue_result.trigger_outcome is RunOutcome.REVIEW_CYCLES_EXHAUSTED
    assert issue_result.expected_exit_code == 20
    assert lease.released is True


def test_finalize_run_quarantines_and_marks_indeterminate_when_termination_is_unconfirmed(
    tmp_path: Path,
) -> None:
    """A child process group that might still be alive makes a
    contemporaneous Git probe unreliable: the postflight determination is
    forced to `INDETERMINATE` for gate purposes even though the fresh probe
    itself came back clean, and the target is quarantined before the lease
    is released (System Design SS16.3; ADR-006) -- but the raw probe
    evidence is still preserved verbatim on `git_postflight`."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")

    checks = _happy_path_git_checks(target.root)
    raw_postflight = _git_check(
        target_root=target.root,
        sequence=6,
        purpose="postflight",
        state=_git_state(target_root=target.root, fingerprint="fp-9"),
        safety_status=GitSafetyStatus.SAFE,
    )
    checks.append(raw_postflight)
    git_safety = SequencedGitSafetyPort(checks)
    run_store = SequencedRunStorePort()
    lease = RecordingTargetLease()

    orchestrator, result = _run_happy_path(
        workspace=workspace,
        target=target,
        git_baseline=git_baseline,
        git_safety=git_safety,
        run_store=run_store,
    )

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=False,
        termination_confirmed=False,
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert lease.quarantine_reasons != []
    assert lease.released is True
    assert issue_result.final_status is FinalStatus.FAILED
    assert issue_result.git_safety_status is GitSafetyStatus.INDETERMINATE

    final_record = run_store.persist_calls[-1]
    # The raw probe evidence survives untouched...
    assert final_record.git_postflight == raw_postflight
    assert final_record.git_postflight is not None
    assert final_record.git_postflight.safety_status is GitSafetyStatus.SAFE
    # ...even though the run's own overall status is downgraded.
    assert final_record.git_safety_status is GitSafetyStatus.INDETERMINATE


def test_finalize_run_records_a_visible_error_when_quarantine_itself_fails(
    tmp_path: Path,
) -> None:
    """A quarantine write failure (System Design SS16.3) is never raised out
    of `finalize_run` and never silently swallowed either -- it stays
    visible on the persisted record's own `errors`, and finalization still
    completes and releases the lease."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")

    checks = _happy_path_git_checks(target.root)
    checks.append(
        _git_check(
            target_root=target.root,
            sequence=6,
            purpose="postflight",
            state=_git_state(target_root=target.root, fingerprint="fp-9"),
        )
    )
    git_safety = SequencedGitSafetyPort(checks)
    run_store = SequencedRunStorePort()
    quarantine_error = LoggingError(
        "locking.quarantine_write_failed", "could not write the quarantine marker"
    )
    lease = RecordingTargetLease(quarantine_error=quarantine_error)

    orchestrator, result = _run_happy_path(
        workspace=workspace,
        target=target,
        git_baseline=git_baseline,
        git_safety=git_safety,
        run_store=run_store,
    )

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=False,
        termination_confirmed=False,
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert lease.quarantine_reasons != []
    assert lease.released is True  # still released despite the failed quarantine
    assert issue_result.final_status is FinalStatus.FAILED

    final_record = run_store.persist_calls[-1]
    assert any(
        error.code == "locking.quarantine_write_failed" for error in final_record.errors
    )


def test_finalize_run_falls_back_to_the_last_durable_record_when_its_own_persist_fails(
    tmp_path: Path,
) -> None:
    """When `finalize_run`'s own last write fails, its `IssueResult` falls
    back to the record it was given -- already durable when this call was
    made -- rather than ever reporting an unpersisted `APPROVED` as genuine
    (System Design SS15.4)."""

    workspace, target = _workspace_and_target(tmp_path)
    git_baseline = _git_state(target_root=target.root, fingerprint="fp-0")

    checks = _happy_path_git_checks(target.root)
    checks.append(
        _git_check(
            target_root=target.root,
            sequence=6,
            purpose="postflight",
            state=_git_state(target_root=target.root, fingerprint="fp-9"),
        )
    )
    git_safety = SequencedGitSafetyPort(checks)
    run_store = SequencedRunStorePort(
        persist_results=[
            PersistenceStatus.OK,
            PersistenceStatus.OK,
            PersistenceStatus.OK,
            PersistenceStatus.FAILED,
        ]
    )
    lease = RecordingTargetLease()

    orchestrator, result = _run_happy_path(
        workspace=workspace,
        target=target,
        git_baseline=git_baseline,
        git_safety=git_safety,
        run_store=run_store,
    )
    last_durable_record = orchestrator.record

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=False,
        termination_confirmed=_termination_confirmed(result),
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert len(run_store.persist_calls) == 4
    assert issue_result.final_status is FinalStatus.FAILED
    assert issue_result.persistence_status is PersistenceStatus.FAILED
    assert issue_result.run_id == last_durable_record.run_id
    assert issue_result.artifact_path == last_durable_record.artifact_path
    assert lease.released is True  # still released even though this write failed


def test_finalize_run_reports_a_provider_exhaustion_trigger_as_failed(
    tmp_path: Path,
) -> None:
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
    checks = [
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
        checks.append(
            _git_check(
                target_root=target.root,
                sequence=len(checks),
                purpose=f"CODER:1:{attempt}:before",
                state=same_fingerprint,
            )
        )
        checks.append(
            _git_check(
                target_root=target.root,
                sequence=len(checks),
                purpose=f"CODER:1:{attempt}:after",
                state=same_fingerprint,
            )
        )
    checks.append(
        _git_check(
            target_root=target.root,
            sequence=len(checks),
            purpose="postflight",
            state=same_fingerprint,
        )
    )
    git_safety = SequencedGitSafetyPort(checks)
    run_store = SequencedRunStorePort()
    lease = RecordingTargetLease()
    orchestrator = _orchestrator(
        workspace=workspace,
        target=target,
        agent_runner=agent_runner,
        git_safety=git_safety,
        run_store=run_store,
        git_baseline=git_baseline,
        sleeper=RecordingSleeper(),
    )

    result = run_issue_pipeline(
        orchestrator=orchestrator,
        issue_locator=_issue_locator(),
        workspace=workspace,
        target=target,
        max_review_cycles=MAX_REVIEW_CYCLES,
    )
    assert result.outcome is RunOutcome.PROVIDER_ERROR
    assert result.reviewer_cycles == ()  # the reviewer never ran

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=False,
        termination_confirmed=_termination_confirmed(result),
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert _last_review_status(result) is None  # no reviewer verdict to weigh
    assert issue_result.final_status is FinalStatus.FAILED
    assert issue_result.trigger_outcome is RunOutcome.PROVIDER_ERROR
    assert issue_result.expected_exit_code == 20
    assert lease.released is True


def test_finalize_run_lets_an_interruption_outrank_the_original_trigger(
    tmp_path: Path,
) -> None:
    """`interrupted=True` outranks a plain technical trigger in
    `resolve_terminal_outcome`'s own precedence (System Design SS13.3),
    exactly as it would for any other terminal path this function converges."""

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
                purpose="postflight",
                state=same_fingerprint,
            ),
        ]
    )
    run_store = SequencedRunStorePort()
    lease = RecordingTargetLease()
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
    assert result.outcome is RunOutcome.AGENT_REPORTED_FAILURE

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=True,
        termination_confirmed=_termination_confirmed(result),
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert issue_result.final_status is FinalStatus.FAILED
    assert issue_result.trigger_outcome is RunOutcome.INTERRUPTED
    assert lease.released is True


def test_finalize_run_reports_a_content_only_drift_after_approval_as_git_safety_error(
    tmp_path: Path,
) -> None:
    """The postflight continuity check compares against the *last checkpoint
    the pipeline itself accepted* -- the reviewer's own `SAFE` `after` -- not
    the run's original baseline with the coder's own tolerance. A
    content-only change to a file the coder never touched, slipped in after
    the reviewer's `APPROVED` and before finalization, changes nothing about
    branch/HEAD but is still `UNSAFE`: comparing against the *original*
    baseline with `role=CODER` would instead tolerate it as an ordinary
    coder delta and mask it entirely (the bug this test guards against)."""

    workspace, target = _workspace_and_target(tmp_path)
    baseline_state = _git_state(target_root=target.root, fingerprint="fp-0")
    after_coder_state = _git_state(target_root=target.root, fingerprint="fp-a")
    drifted_after_approval_state = _git_state(
        target_root=target.root, fingerprint="fp-ab"
    )

    git_safety = ComparingGitSafetyPort(
        [
            baseline_state,  # architect before: no drift yet
            baseline_state,  # architect after: architect changes nothing
            baseline_state,  # coder before: continuity holds
            after_coder_state,  # coder after: A changes, tolerated for CODER
            after_coder_state,  # reviewer before: continuity holds
            after_coder_state,  # reviewer after: reviewer changes nothing
            drifted_after_approval_state,  # postflight: B changes, content-only
        ]
    )
    run_store = SequencedRunStorePort()
    lease = RecordingTargetLease()

    orchestrator, result = _run_happy_path(
        workspace=workspace,
        target=target,
        git_baseline=baseline_state,
        git_safety=git_safety,
        run_store=run_store,
    )
    assert _last_review_status(result) is ReviewStatus.APPROVED

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=False,
        termination_confirmed=_termination_confirmed(result),
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    postflight_call = git_safety.calls[-1]
    _, purpose, role, compared_baseline = postflight_call
    assert purpose == "postflight"
    assert role is None
    assert compared_baseline == after_coder_state  # the last *accepted* checkpoint...
    assert compared_baseline != baseline_state  # ...never the original baseline

    assert _last_review_status(result) is ReviewStatus.APPROVED  # unchanged, historical
    assert issue_result.git_safety_status is GitSafetyStatus.UNSAFE
    assert issue_result.final_status is FinalStatus.FAILED
    assert issue_result.trigger_outcome is RunOutcome.GIT_SAFETY_ERROR
    assert issue_result.expected_exit_code == 30
    assert issue_result.changes_preserved is True  # A and B both stay on disk
    assert lease.released is True


def test_finalize_run_postflight_stays_safe_with_no_new_delta_after_approval(
    tmp_path: Path,
) -> None:
    """The mirror image of the drift case above: when nothing at all changes
    between the reviewer's `APPROVED` and finalization, the postflight
    continuity check against the last accepted checkpoint still comes back
    `SAFE`, and the run finalizes `APPROVED`."""

    workspace, target = _workspace_and_target(tmp_path)
    baseline_state = _git_state(target_root=target.root, fingerprint="fp-0")
    after_coder_state = _git_state(target_root=target.root, fingerprint="fp-a")

    git_safety = ComparingGitSafetyPort(
        [
            baseline_state,  # architect before
            baseline_state,  # architect after
            baseline_state,  # coder before
            after_coder_state,  # coder after: A changes, tolerated for CODER
            after_coder_state,  # reviewer before
            after_coder_state,  # reviewer after
            after_coder_state,  # postflight: nothing changed since approval
        ]
    )
    run_store = SequencedRunStorePort()
    lease = RecordingTargetLease()

    orchestrator, result = _run_happy_path(
        workspace=workspace,
        target=target,
        git_baseline=baseline_state,
        git_safety=git_safety,
        run_store=run_store,
    )

    issue_result = finalize_run(
        record=orchestrator.record,
        target=target,
        trigger_outcome=_trigger_outcome(result),
        review_status=_last_review_status(result),
        interrupted=False,
        termination_confirmed=_termination_confirmed(result),
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=MAX_REVIEW_CYCLES,
    )

    assert issue_result.git_safety_status is GitSafetyStatus.SAFE
    assert issue_result.final_status is FinalStatus.APPROVED
    assert issue_result.expected_exit_code == 0
    assert lease.released is True

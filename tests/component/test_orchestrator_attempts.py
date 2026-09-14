"""Component tests for the attempt sequence and technical precedence (M12-02).

Drives `orchestrator.IssueOrchestrator` across a short, realistic sequence of
logical invocations (architect, a coder/reviewer cycle, a rework cycle, and a
retried provider attempt) using real `tmp_path`-derived absolute paths for
`Workspace`/`TargetRepository`, exactly as a composed run would see them,
while every port stays a fake: M12-02 explicitly excludes concrete adapters
and a real retry/rework loop. This file proves the Git checkpoint continuity
chain (each attempt's "before" equals the last *accepted* "after") and the
attempt classification hold across more than one invocation, including a
drifted checkpoint that must never become the new trusted baseline.
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
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    ReviewStatus,
    RunOutcome,
    RunRecord,
    TargetRepository,
    Workspace,
)
from opencode_tools.orchestrator import (
    InvocationEventKind,
    IssueOrchestrator,
    LogicalInvocationResult,
)
from opencode_tools.ports import AttemptLogSink, LogChannel

_PHASE_BY_ROLE: dict[AgentRole, PipelinePhase] = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}

NOW = datetime(2026, 9, 14, 9, 0, 0, tzinfo=UTC)


def _agent_process_result(*, workspace_root: Path) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/opencode", "run"),
        cwd=workspace_root,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=5),
        duration_ns=5_000_000_000,
        return_code=0,
        timed_out=False,
        termination_confirmed=True,
        log_path=Path("attempt.log"),
        stdout_byte_count=0,
        stdout_sha256="stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="stderr-digest",
        outcome=RunOutcome.SUCCEEDED,
    )


def _success_response(role: AgentRole) -> ParsedAgentResponse:
    if role is AgentRole.ARCHITECT:
        return ParsedAgentResponse(
            role=role, body="ready", agent_status=AgentStatus.READY
        )
    if role is AgentRole.CODER:
        return ParsedAgentResponse(
            role=role, body="done", agent_status=AgentStatus.COMPLETED
        )
    return ParsedAgentResponse(
        role=role, body="approved", review_status=ReviewStatus.APPROVED
    )


def _agent_result(
    *,
    role: AgentRole,
    review_cycle: int | None,
    provider_attempt: int,
    workspace_root: Path,
) -> AgentResult:
    return AgentResult(
        role=role,
        phase=_PHASE_BY_ROLE[role],
        review_cycle=review_cycle,
        provider_attempt=provider_attempt,
        process=_agent_process_result(workspace_root=workspace_root),
        terminal_response=_success_response(role),
        session_id=f"session-{role.value.lower()}-{review_cycle or 0}-{provider_attempt}",
        verified_agent=role.value.lower(),
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
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
    """A `RunStorePort` fake that opens one fresh sink per attempt."""

    def __init__(self) -> None:
        self.sink_calls: list[tuple[AgentRole, int | None, int]] = []
        self.opened_sinks: list[RecordingAttemptLogSink] = []

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        raise AssertionError("not exercised by the M12-02 skeleton")

    def open_attempt_sink(
        self,
        role: AgentRole,
        review_cycle: int | None,
        provider_attempt: int,
    ) -> AttemptLogSink:
        self.sink_calls.append((role, review_cycle, provider_attempt))
        cycle_component = review_cycle if review_cycle is not None else 0
        sink = RecordingAttemptLogSink(
            path=Path(f"{role.value.lower()}-{cycle_component}-{provider_attempt}.log")
        )
        self.opened_sinks.append(sink)
        return sink

    def persist(self, record: RunRecord) -> PersistenceStatus:
        raise AssertionError("not exercised by the M12-02 skeleton")


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
        raise AssertionError("not exercised by the M12-02 skeleton")

    def resolve_target(
        self, workspace: Workspace, target_root: Path
    ) -> TargetRepository:
        raise AssertionError("not exercised by the M12-02 skeleton")

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


class SteppingClock:
    """A `Clock` fake whose `now()` advances by one second on every call."""

    def __init__(self, *, start: datetime) -> None:
        self._next = start

    def now(self) -> datetime:
        current = self._next
        self._next = current + timedelta(seconds=1)
        return current

    def monotonic_ns(self) -> int:
        raise AssertionError("not exercised by the M12-02 skeleton")


def _git_state(*, target_root: Path, fingerprint: str) -> GitState:
    return GitState(
        root=target_root,
        branch="main",
        head="deadbeef",
        porcelain_summary="clean",
        staged=(),
        unstaged=(),
        untracked=(),
        fingerprint=fingerprint,
    )


def _git_check(
    *,
    target_root: Path,
    sequence: int,
    purpose: str,
    fingerprint: str,
    safety_status: GitSafetyStatus = GitSafetyStatus.SAFE,
) -> GitCheckRecord:
    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=(_git_probe_result(target_root=target_root),),
        state=_git_state(target_root=target_root, fingerprint=fingerprint),
        safety_status=safety_status,
    )


def test_logical_invocations_across_a_rework_cycle_keep_checkpoint_continuity(
    tmp_path: Path,
) -> None:
    """A skeleton architect -> coder -> reviewer -> coder rework sequence.

    No adapter or subprocess is ever constructed; every side effect goes
    through the injected `AgentRunner`/`RunStorePort`/`GitSafetyPort`/`Clock`
    fakes. Proves each logical invocation's Git "before" checkpoint is
    continuous with the previous one's "after" (never a fresh, isolated
    snapshot), that a drifted checkpoint is detected but never becomes the
    next trusted baseline, and that identity/counters remain unambiguous
    across a provider retry.
    """

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

    plan = [
        (AgentRole.ARCHITECT, None, 1),
        (AgentRole.CODER, 1, 1),
        (AgentRole.REVIEWER, 1, 1),
        (AgentRole.CODER, 2, 1),
        (AgentRole.CODER, 2, 2),  # a provider retry of the same logical invocation
    ]
    agent_results = [
        _agent_result(
            role=role,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            workspace_root=workspace_root,
        )
        for role, review_cycle, provider_attempt in plan
    ]

    # One SAFE (before, after) pair per attempt, except the reviewer's after
    # checkpoint, which drifts (UNSAFE) -- a read-only role's mutation -- and
    # must not become the next attempt's trusted baseline.
    git_results: list[GitCheckRecord] = []
    fingerprint = 0
    for index, (role, review_cycle, provider_attempt) in enumerate(plan):
        git_results.append(
            _git_check(
                target_root=target_root,
                sequence=len(git_results),
                purpose=f"{role.value}:{review_cycle}:{provider_attempt}:before",
                fingerprint=f"fp-{fingerprint}",
            )
        )
        fingerprint += 1
        is_reviewer_after = role is AgentRole.REVIEWER
        git_results.append(
            _git_check(
                target_root=target_root,
                sequence=len(git_results),
                purpose=f"{role.value}:{review_cycle}:{provider_attempt}:after",
                fingerprint=f"fp-{fingerprint}",
                safety_status=(
                    GitSafetyStatus.UNSAFE
                    if is_reviewer_after
                    else GitSafetyStatus.SAFE
                ),
            )
        )
        fingerprint += 1

    run_store = SequencedRunStorePort()
    agent_runner = SequencedAgentRunner(agent_results)
    git_safety = SequencedGitSafetyPort(git_results)
    orchestrator = IssueOrchestrator(
        run_id="run-2026-09-14-abcd",
        target=target,
        agent_runner=agent_runner,
        run_store=run_store,
        git_safety=git_safety,
        clock=SteppingClock(start=NOW),
    )

    outcomes: list[LogicalInvocationResult] = [
        orchestrator.run_logical_invocation(
            role=role,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            prompt=f"Attempt {provider_attempt} of {role.value} cycle {review_cycle}.",
            workspace=workspace,
        )
        for role, review_cycle, provider_attempt in plan
    ]

    architect, coder_1, reviewer_1, coder_2_attempt_1, coder_2_attempt_2 = outcomes

    # Every provider attempt actually reached the agent runner, in the exact
    # planned order, carrying the real workspace and its own attempt sink.
    assert [
        (role, review_cycle, provider_attempt)
        for role, _prompt, _workspace, review_cycle, provider_attempt in (
            agent_runner.calls
        )
    ] == [(role, cycle, attempt) for role, cycle, attempt in plan]
    assert all(call[2] == workspace for call in agent_runner.calls)
    assert run_store.sink_calls == [
        (role, cycle, attempt) for role, cycle, attempt in plan
    ]
    assert len(run_store.opened_sinks) == 5
    assert all(sink.closed for sink in run_store.opened_sinks)

    # Git continuity: every "before" (after the very first) equals the last
    # ACCEPTED "after" GitState -- and the reviewer's drifted after is never
    # accepted, so both the following coder attempts chain from coder-1's
    # after instead. `git_results` is consumed in exact call order (before,
    # after, before, after, ...), so index arithmetic gives the returned
    # `GitCheckRecord` each call actually produced.
    before_calls = [call for call in git_safety.calls if call[2].endswith(":before")]
    after_calls = [call for call in git_safety.calls if call[2].endswith(":after")]
    assert len(before_calls) == 5
    assert len(after_calls) == 5
    architect_after, coder_1_after, reviewer_after, coder_2_attempt_1_after = (
        git_results[1],
        git_results[3],
        git_results[5],
        git_results[7],
    )
    assert reviewer_after.safety_status is GitSafetyStatus.UNSAFE
    assert before_calls[0][4] is None  # the very first checkpoint has no baseline
    assert before_calls[1][4] is architect_after.state  # coder-1 continues architect
    assert before_calls[2][4] is coder_1_after.state  # reviewer continues coder-1
    # coder-2 attempt 1 continues from coder-1's SAFE after, not the
    # reviewer's drifted (UNSAFE, never accepted) one
    assert before_calls[3][4] is coder_1_after.state
    # the provider retry's own before continues from coder-2 attempt 1's own
    # SAFE after, since Git safety and technical outcome are orthogonal
    assert before_calls[4][4] is coder_2_attempt_1_after.state
    assert all(call[3] is None for call in before_calls)  # role=None for continuity
    assert [call[3] for call in after_calls] == [role for role, _c, _a in plan]

    # Every logical invocation is unambiguous: role/cycle pairs that differ
    # get distinct identities, and identity survives a provider retry.
    ids = [outcome.logical_invocation_id for outcome in outcomes]
    assert len(set(ids[:4])) == 4
    assert (
        coder_2_attempt_1.logical_invocation_id
        == coder_2_attempt_2.logical_invocation_id
    )
    assert coder_2_attempt_1.provider_attempt == 1
    assert coder_2_attempt_2.provider_attempt == 2

    # Role/phase/cycle/provider-attempt remain distinct, orthogonal fields,
    # and technical precedence is SUCCEEDED for every attempt in this plan
    # even though the reviewer's Git safety dimension drifted independently.
    assert architect.state.phase is PipelinePhase.ARCHITECT
    assert architect.state.review_cycle is None
    assert coder_1.state.phase is PipelinePhase.CODER
    assert coder_1.state.review_cycle == 1
    assert reviewer_1.state.phase is PipelinePhase.REVIEWER
    assert reviewer_1.state.review_cycle == 1
    assert reviewer_1.precedence is not None
    assert reviewer_1.precedence.outcome is RunOutcome.SUCCEEDED
    assert reviewer_1.git_after is not None
    assert reviewer_1.git_after.safety_status is GitSafetyStatus.UNSAFE
    assert coder_2_attempt_1.state.review_cycle == 2
    assert coder_2_attempt_2.state.review_cycle == 2

    # Each invocation's own event trail is complete and strictly ordered.
    for outcome in outcomes:
        assert [event.kind for event in outcome.events] == [
            InvocationEventKind.INVOCATION_STARTED,
            InvocationEventKind.ATTEMPT_SINK_OPENED,
            InvocationEventKind.GIT_BEFORE_CHECKED,
            InvocationEventKind.AGENT_RESULT_RECEIVED,
            InvocationEventKind.GIT_AFTER_CHECKED,
            InvocationEventKind.ATTEMPT_SINK_CLOSED,
        ]
        assert [event.sequence for event in outcome.events] == [0, 1, 2, 3, 4, 5]
        timestamps = [event.timestamp for event in outcome.events]
        assert timestamps == sorted(timestamps)


def test_a_git_before_drift_blocks_the_agent_and_the_run_store_still_opened_a_sink(
    tmp_path: Path,
) -> None:
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

    drifted_before = _git_check(
        target_root=target_root,
        sequence=0,
        purpose="coder:1:1:before",
        fingerprint="fp-drift",
        safety_status=GitSafetyStatus.UNSAFE,
    )
    run_store = SequencedRunStorePort()
    agent_runner = SequencedAgentRunner([])
    git_safety = SequencedGitSafetyPort([drifted_before])
    orchestrator = IssueOrchestrator(
        run_id="run-2026-09-14-efgh",
        target=target,
        agent_runner=agent_runner,
        run_store=run_store,
        git_safety=git_safety,
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    assert agent_runner.calls == []
    assert run_store.sink_calls == [(AgentRole.CODER, 1, 1)]
    assert run_store.opened_sinks[0].closed is True
    assert result.git_before is drifted_before
    assert result.agent_result is None
    assert result.git_after is None
    assert result.precedence is None
    assert [event.kind for event in result.events] == [
        InvocationEventKind.INVOCATION_STARTED,
        InvocationEventKind.ATTEMPT_SINK_OPENED,
        InvocationEventKind.GIT_BEFORE_CHECKED,
        InvocationEventKind.ATTEMPT_SINK_CLOSED,
        InvocationEventKind.BLOCKED_BY_GIT_SAFETY,
    ]

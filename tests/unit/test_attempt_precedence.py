"""Unit tests for the attempt sequence and technical precedence (M12-02).

Exercises `orchestrator.IssueOrchestrator` purely against `Fake`/`Recording`
doubles written directly against `ports.py` `Protocol`s -- no subprocess, no
real clock, no real Git or persistence. Covers M12-02's acceptance criteria:
the observable, invariant order of operations (exclusive sink, continuous
Git "before", agent run, Git "after" unconditionally, technical
classification); every failure still performing its Git check; a
higher-precedence outcome never being replaced while concurrent diagnostics
are preserved; the attempt's classification never being swayed by a
`terminal_response` a higher-precedence signal already forbids reading; and
Git control-plane/branch/HEAD drift blocking before the next role. Retry,
persistence, and cancellation remain out of scope (M12-03/M12-04).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

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
    ProviderDiagnostic,
    ReviewStatus,
    RunOutcome,
    RunRecord,
    TargetRepository,
    Workspace,
)
from opencode_tools.orchestrator import (
    InvocationEvent,
    InvocationEventKind,
    IssueOrchestrator,
    LogicalInvocationResult,
)
from opencode_tools.ports import AttemptLogSink, LogChannel
from opencode_tools.state_machine import AttemptPrecedence, PipelineState

_ALLOWED_INTERNAL_MODULES = frozenset(
    {
        "opencode_tools.domain",
        "opencode_tools.ports",
        "opencode_tools.state_machine",
        "opencode_tools.retry",
        "opencode_tools.prompting",
    }
)
_ORCHESTRATOR_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "opencode_tools" / "orchestrator.py"
)

_PHASE_BY_ROLE: dict[AgentRole, PipelinePhase] = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}

NOW = datetime(2026, 9, 14, 9, 0, 0, tzinfo=UTC)
WORKSPACE_ROOT = Path("/workspaces/opencode-tools")
TARGET_ROOT = WORKSPACE_ROOT / "backend"


def _orchestrator_module_imports() -> frozenset[str]:
    tree = ast.parse(
        _ORCHESTRATOR_SOURCE.read_text(encoding="utf-8"),
        filename=str(_ORCHESTRATOR_SOURCE),
    )
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise AssertionError("orchestrator.py must not use relative imports")
            if node.module is not None:
                modules.add(node.module)
    return frozenset(modules)


def _workspace() -> Workspace:
    return Workspace(root=WORKSPACE_ROOT)


def _target() -> TargetRepository:
    return TargetRepository(
        root=TARGET_ROOT,
        workspace_relative=Path("backend"),
        git_common_dir=TARGET_ROOT / ".git",
    )


def _git_probe_result() -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/git", "status"),
        cwd=TARGET_ROOT,
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


def _git_state(*, fingerprint: str | None = "fingerprint-0") -> GitState:
    return GitState(
        root=TARGET_ROOT,
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
    sequence: int,
    purpose: str,
    safety_status: GitSafetyStatus = GitSafetyStatus.SAFE,
    state: GitState | None = None,
    compared_to: str | None = None,
) -> GitCheckRecord:
    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=(_git_probe_result(),),
        state=state if state is not None else _git_state(),
        safety_status=safety_status,
        compared_to=compared_to,
    )


def _agent_process_result(
    *,
    outcome: RunOutcome = RunOutcome.SUCCEEDED,
    timed_out: bool = False,
    return_code: int | None = 0,
) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/opencode", "run"),
        cwd=WORKSPACE_ROOT,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=5),
        duration_ns=5_000_000_000,
        return_code=return_code,
        timed_out=timed_out,
        termination_confirmed=True,
        log_path=Path("attempt.log"),
        stdout_byte_count=0,
        stdout_sha256="stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="stderr-digest",
        outcome=outcome,
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


def _failure_response(role: AgentRole) -> ParsedAgentResponse:
    return ParsedAgentResponse(
        role=role, body="could not complete", agent_status=AgentStatus.FAILED
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
    provider_attempt: int,
    process: ProcessResult | None = None,
    terminal_response: ParsedAgentResponse | None = None,
    provider_diagnostic: ProviderDiagnostic | None = None,
    outcome: RunOutcome = RunOutcome.SUCCEEDED,
) -> AgentResult:
    return AgentResult(
        role=role,
        phase=_PHASE_BY_ROLE[role],
        review_cycle=review_cycle,
        provider_attempt=provider_attempt,
        process=process if process is not None else _agent_process_result(),
        terminal_response=terminal_response,
        session_id="session-001",
        verified_agent=role.value.lower(),
        provider_diagnostic=provider_diagnostic,
        outcome=outcome,
    )


def _pipeline_state(*, role: AgentRole, review_cycle: int | None) -> PipelineState:
    return PipelineState(phase=_PHASE_BY_ROLE[role], review_cycle=review_cycle)


class RecordingAttemptLogSink:
    """A minimal `AttemptLogSink` fake that records writes and closing."""

    def __init__(self, path: Path = Path("attempt.log")) -> None:
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


class RecordingRunStorePort:
    """A `RunStorePort` fake that appends to a shared cross-port call log."""

    def __init__(self, call_log: list[str], *, sink: RecordingAttemptLogSink) -> None:
        self._call_log = call_log
        self._sink = sink
        self.sink_calls: list[tuple[AgentRole, int | None, int]] = []

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        raise AssertionError("not exercised by the M12-02 skeleton")

    def open_attempt_sink(
        self,
        role: AgentRole,
        review_cycle: int | None,
        provider_attempt: int,
    ) -> AttemptLogSink:
        self._call_log.append("run_store.open_attempt_sink")
        self.sink_calls.append((role, review_cycle, provider_attempt))
        return self._sink

    def persist(self, record: RunRecord) -> PersistenceStatus:
        raise AssertionError("not exercised by the M12-02 skeleton")


class ScriptedAgentRunner:
    """An `AgentRunner` fake that appends to a shared cross-port call log."""

    def __init__(self, call_log: list[str], *, result: AgentResult) -> None:
        self._call_log = call_log
        self._result = result
        self.calls: list[
            tuple[AgentRole, str, Workspace, int | None, int, AttemptLogSink]
        ] = []

    def queue_result(self, result: AgentResult) -> None:
        """Replace the canned result returned by the next `run()` call."""
        self._result = result

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
        self._call_log.append("agent_runner.run")
        self.calls.append(
            (role, prompt, workspace, review_cycle, provider_attempt, sink)
        )
        return self._result


class RecordingGitSafetyPort:
    """A `GitSafetyPort` fake returning one scripted check result per call."""

    def __init__(self, call_log: list[str], results: list[GitCheckRecord]) -> None:
        self._call_log = call_log
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
        self._call_log.append("git_safety.check")
        self.calls.append((target, sequence, purpose, role, baseline))
        return self._results.pop(0)


class SteppingClock:
    """A `Clock` fake whose `now()` advances by one second on every call."""

    def __init__(self, *, start: datetime) -> None:
        self._next = start
        self.calls = 0

    def now(self) -> datetime:
        current = self._next
        self._next = current + timedelta(seconds=1)
        self.calls += 1
        return current

    def monotonic_ns(self) -> int:
        raise AssertionError("not exercised by the M12-02 skeleton")


def _orchestrator(
    *,
    call_log: list[str],
    sink: RecordingAttemptLogSink,
    agent_result: AgentResult,
    git_results: list[GitCheckRecord],
    clock: SteppingClock,
    run_id: str = "run-001",
    target: TargetRepository | None = None,
) -> tuple[
    IssueOrchestrator,
    RecordingRunStorePort,
    ScriptedAgentRunner,
    RecordingGitSafetyPort,
]:
    run_store = RecordingRunStorePort(call_log, sink=sink)
    agent_runner = ScriptedAgentRunner(call_log, result=agent_result)
    git_safety = RecordingGitSafetyPort(call_log, git_results)
    orchestrator = IssueOrchestrator(
        run_id=run_id,
        target=target if target is not None else _target(),
        agent_runner=agent_runner,
        run_store=run_store,
        git_safety=git_safety,
        clock=clock,
    )
    return orchestrator, run_store, agent_runner, git_safety


def _safe_pair(*, attempt_fingerprint: str = "fingerprint-1") -> list[GitCheckRecord]:
    before = _git_check(sequence=0, purpose="before", state=_git_state())
    after = _git_check(
        sequence=1,
        purpose="after",
        state=_git_state(fingerprint=attempt_fingerprint),
        compared_to="before",
    )
    return [before, after]


# --- Import boundary --------------------------------------------------------


def test_orchestrator_module_imports_only_domain_ports_and_pure_policy() -> None:
    imports = _orchestrator_module_imports()

    internal = {name for name in imports if name.startswith("opencode_tools")}
    assert internal, "orchestrator.py must depend on at least one internal module"
    assert internal <= _ALLOWED_INTERNAL_MODULES


# --- Dependency injection ----------------------------------------------------


def test_orchestrator_is_constructed_entirely_from_injected_ports() -> None:
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    result = _agent_result(
        role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
    )
    clock = SteppingClock(start=NOW)

    orchestrator, run_store, agent_runner, git_safety = _orchestrator(
        call_log=call_log,
        sink=sink,
        agent_result=result,
        git_results=_safe_pair(),
        clock=clock,
    )

    assert isinstance(orchestrator, IssueOrchestrator)
    assert run_store.sink_calls == []
    assert agent_runner.calls == []
    assert git_safety.calls == []


def test_orchestrator_rejects_empty_run_id() -> None:
    with pytest.raises(ValueError, match="run_id"):
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
            run_id="",
        )


def test_orchestrator_rejects_a_target_of_the_wrong_type() -> None:
    with pytest.raises(TypeError, match="target"):
        _orchestrator(
            call_log=[],
            sink=RecordingAttemptLogSink(),
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
            ),
            git_results=_safe_pair(),
            clock=SteppingClock(start=NOW),
            target=cast(TargetRepository, TARGET_ROOT),
        )


# --- Order of operations and call recording ---------------------------------


def test_run_logical_invocation_follows_the_canonical_order_when_safe() -> None:
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=2,
        provider_attempt=1,
        terminal_response=_success_response(AgentRole.CODER),
    )
    clock = SteppingClock(start=NOW)
    target = _target()
    git_results = _safe_pair()
    orchestrator, run_store, agent_runner, git_safety = _orchestrator(
        call_log=call_log,
        sink=sink,
        agent_result=agent_result,
        git_results=git_results,
        clock=clock,
        target=target,
    )
    workspace = _workspace()

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=2,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    assert call_log == [
        "run_store.open_attempt_sink",
        "git_safety.check",
        "agent_runner.run",
        "git_safety.check",
    ]
    assert run_store.sink_calls == [(AgentRole.CODER, 2, 1)]
    assert agent_runner.calls == [
        (AgentRole.CODER, "Implement the fix.", workspace, 2, 1, sink)
    ]
    assert sink.closed is True
    assert [(call[0], call[3], call[4]) for call in git_safety.calls] == [
        (target, None, None),
        (target, AgentRole.CODER, git_results[0].state),
    ]
    assert result.git_before is git_results[0]
    assert result.git_after is git_results[1]
    assert result.agent_result is agent_result
    assert [event.kind for event in result.events] == [
        InvocationEventKind.INVOCATION_STARTED,
        InvocationEventKind.ATTEMPT_SINK_OPENED,
        InvocationEventKind.GIT_BEFORE_CHECKED,
        InvocationEventKind.AGENT_RESULT_RECEIVED,
        InvocationEventKind.GIT_AFTER_CHECKED,
        InvocationEventKind.ATTEMPT_SINK_CLOSED,
    ]
    assert [event.sequence for event in result.events] == [0, 1, 2, 3, 4, 5]
    timestamps = [event.timestamp for event in result.events]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == 6


def test_run_logical_invocation_before_check_passes_role_none_and_after_passes_role() -> (
    None
):
    call_log: list[str] = []
    orchestrator, _run_store, _agent_runner, git_safety = _orchestrator(
        call_log=call_log,
        sink=RecordingAttemptLogSink(),
        agent_result=_agent_result(
            role=AgentRole.ARCHITECT,
            review_cycle=None,
            provider_attempt=1,
            terminal_response=_success_response(AgentRole.ARCHITECT),
        ),
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        prompt="Design the fix.",
        workspace=_workspace(),
    )

    before_call, after_call = git_safety.calls
    assert before_call[3] is None
    assert after_call[3] is AgentRole.ARCHITECT


# --- Git continuity across invocations ---------------------------------------


def test_run_logical_invocation_before_baseline_is_the_last_accepted_after_state() -> (
    None
):
    call_log: list[str] = []
    clock = SteppingClock(start=NOW)
    workspace = _workspace()
    first_pair = _safe_pair(attempt_fingerprint="fingerprint-after-1")
    second_pair = [
        _git_check(sequence=2, purpose="before-2", state=first_pair[1].state),
        _git_check(
            sequence=3,
            purpose="after-2",
            state=_git_state(fingerprint="fingerprint-after-2"),
        ),
    ]
    orchestrator, _run_store, agent_runner, git_safety = _orchestrator(
        call_log=call_log,
        sink=RecordingAttemptLogSink(),
        agent_result=_agent_result(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=1,
            terminal_response=_success_response(AgentRole.CODER),
        ),
        git_results=[*first_pair, *second_pair],
        clock=clock,
    )

    orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )
    agent_runner.queue_result(
        _agent_result(
            role=AgentRole.REVIEWER,
            review_cycle=1,
            provider_attempt=1,
            terminal_response=_success_response(AgentRole.REVIEWER),
        )
    )
    orchestrator.run_logical_invocation(
        role=AgentRole.REVIEWER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Review the fix.",
        workspace=workspace,
    )

    assert git_safety.calls[0][4] is None  # very first before: no baseline yet
    assert git_safety.calls[2][4] is first_pair[1].state  # continuous with attempt 1


def test_run_logical_invocation_does_not_accept_an_unsafe_after_as_the_new_baseline() -> (
    None
):
    call_log: list[str] = []
    workspace = _workspace()
    drifted_after = _git_check(
        sequence=1,
        purpose="after-1",
        safety_status=GitSafetyStatus.UNSAFE,
        state=_git_state(fingerprint="fingerprint-drift"),
    )
    before_1 = _git_check(sequence=0, purpose="before-1")
    before_2 = _git_check(sequence=2, purpose="before-2")
    after_2 = _git_check(
        sequence=3, purpose="after-2", state=_git_state(fingerprint="fingerprint-2")
    )
    orchestrator, _run_store, agent_runner, git_safety = _orchestrator(
        call_log=call_log,
        sink=RecordingAttemptLogSink(),
        agent_result=_agent_result(
            role=AgentRole.ARCHITECT,
            review_cycle=None,
            provider_attempt=1,
            terminal_response=_success_response(AgentRole.ARCHITECT),
        ),
        git_results=[before_1, drifted_after, before_2, after_2],
        clock=SteppingClock(start=NOW),
    )

    orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        prompt="Design the fix.",
        workspace=workspace,
    )
    agent_runner.queue_result(
        _agent_result(
            role=AgentRole.ARCHITECT,
            review_cycle=None,
            provider_attempt=2,
            terminal_response=_success_response(AgentRole.ARCHITECT),
        )
    )
    orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=2,
        prompt="Design the fix, retried.",
        workspace=workspace,
    )

    # The drifted (unsafe) after-check never becomes the trusted baseline.
    assert git_safety.calls[2][4] is None


# --- Git safety blocks the agent before it is ever invoked -------------------


@pytest.mark.parametrize(
    "before_status", [GitSafetyStatus.UNSAFE, GitSafetyStatus.INDETERMINATE]
)
def test_run_logical_invocation_blocks_the_agent_when_before_is_not_safe(
    before_status: GitSafetyStatus,
) -> None:
    call_log: list[str] = []
    sink = RecordingAttemptLogSink()
    before = _git_check(sequence=0, purpose="before", safety_status=before_status)
    orchestrator, run_store, agent_runner, _git_safety = _orchestrator(
        call_log=call_log,
        sink=sink,
        agent_result=_agent_result(
            role=AgentRole.CODER, review_cycle=1, provider_attempt=1
        ),
        git_results=[before],
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=_workspace(),
    )

    assert call_log == ["run_store.open_attempt_sink", "git_safety.check"]
    assert run_store.sink_calls == [(AgentRole.CODER, 1, 1)]
    assert agent_runner.calls == []
    assert sink.closed is True
    assert result.git_before is before
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


# --- ID / counter invariants -------------------------------------------------


def test_run_logical_invocation_assigns_the_same_identity_across_provider_attempts() -> (
    None
):
    call_log: list[str] = []
    workspace = _workspace()
    orchestrator, _run_store, agent_runner, _git_safety = _orchestrator(
        call_log=call_log,
        sink=RecordingAttemptLogSink(),
        agent_result=_agent_result(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=1,
            outcome=RunOutcome.PROVIDER_ERROR,
            provider_diagnostic=_provider_diagnostic(),
        ),
        git_results=[*_safe_pair(), *_safe_pair()],
        clock=SteppingClock(start=NOW),
    )

    first = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    agent_runner.queue_result(
        _agent_result(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=2,
            terminal_response=_success_response(AgentRole.CODER),
        )
    )
    second = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=2,
        prompt="Implement the fix.",
        workspace=workspace,
    )

    assert first.logical_invocation_id == second.logical_invocation_id
    assert first.provider_attempt == 1
    assert second.provider_attempt == 2


def test_run_logical_invocation_rejects_architect_with_a_review_cycle() -> None:
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=_agent_result(
            role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
        ),
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    with pytest.raises(ValueError, match="review_cycle"):
        orchestrator.run_logical_invocation(
            role=AgentRole.ARCHITECT,
            review_cycle=1,
            provider_attempt=1,
            prompt="prompt",
            workspace=_workspace(),
        )


def test_run_logical_invocation_rejects_coder_without_a_review_cycle() -> None:
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=_agent_result(
            role=AgentRole.CODER, review_cycle=1, provider_attempt=1
        ),
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    with pytest.raises(ValueError, match="review_cycle"):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=None,
            provider_attempt=1,
            prompt="prompt",
            workspace=_workspace(),
        )


def test_run_logical_invocation_rejects_non_positive_provider_attempt() -> None:
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=_agent_result(
            role=AgentRole.CODER, review_cycle=1, provider_attempt=1
        ),
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    with pytest.raises(ValueError, match="provider_attempt"):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=0,
            prompt="prompt",
            workspace=_workspace(),
        )


def test_run_logical_invocation_rejects_wrong_types() -> None:
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=_agent_result(
            role=AgentRole.CODER, review_cycle=1, provider_attempt=1
        ),
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    with pytest.raises(TypeError, match="role"):
        orchestrator.run_logical_invocation(
            role=cast(AgentRole, "CODER"),
            review_cycle=1,
            provider_attempt=1,
            prompt="prompt",
            workspace=_workspace(),
        )
    with pytest.raises(TypeError, match="workspace"):
        orchestrator.run_logical_invocation(
            role=AgentRole.CODER,
            review_cycle=1,
            provider_attempt=1,
            prompt="prompt",
            workspace=cast(Workspace, WORKSPACE_ROOT),
        )


# --- Exhaustive technical precedence -----------------------------------------


def test_precedence_classifies_a_pure_timeout() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(
            outcome=RunOutcome.TIMEOUT, timed_out=True, return_code=None
        ),
    )
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=agent_result,
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.TIMEOUT, concurrent_signals=(RunOutcome.TIMEOUT,)
    )


def test_precedence_classifies_a_pure_provider_error() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        provider_diagnostic=_provider_diagnostic(),
    )
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=agent_result,
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROVIDER_ERROR,
        concurrent_signals=(RunOutcome.PROVIDER_ERROR,),
    )


def test_precedence_classifies_a_pure_process_error() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(outcome=RunOutcome.PROCESS_ERROR, return_code=1),
    )
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=agent_result,
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROCESS_ERROR,
        concurrent_signals=(RunOutcome.PROCESS_ERROR,),
    )


def test_precedence_classifies_a_pure_protocol_error() -> None:
    """Also stands in for a control-plane digest drift (System Design line
    1116): the technical process succeeded, but no parseable terminal
    response is available, which classifies identically -- `PROTOCOL_ERROR`
    -- and blocks the caller from proceeding to the next role.
    """

    agent_result = _agent_result(
        role=AgentRole.CODER, review_cycle=1, provider_attempt=1, terminal_response=None
    )
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=agent_result,
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROTOCOL_ERROR,
        concurrent_signals=(RunOutcome.PROTOCOL_ERROR,),
    )


def test_precedence_classifies_a_pure_agent_reported_failure() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        terminal_response=_failure_response(AgentRole.CODER),
    )
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=agent_result,
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.AGENT_REPORTED_FAILURE,
        concurrent_signals=(RunOutcome.AGENT_REPORTED_FAILURE,),
    )


def test_precedence_classifies_a_pure_success() -> None:
    agent_result = _agent_result(
        role=AgentRole.REVIEWER,
        review_cycle=1,
        provider_attempt=1,
        terminal_response=_success_response(AgentRole.REVIEWER),
    )
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=agent_result,
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.REVIEWER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
    )


def test_precedence_keeps_timeout_over_a_concurrent_provider_signal() -> None:
    """A timeout with a provider-error-like signal in the partial output:
    `TIMEOUT` wins (higher precedence) but the provider signal is preserved
    as a concurrent cause, never silently discarded (AC-031)."""

    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(
            outcome=RunOutcome.TIMEOUT, timed_out=True, return_code=None
        ),
        provider_diagnostic=_provider_diagnostic(),
    )
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=agent_result,
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.TIMEOUT,
        concurrent_signals=(RunOutcome.TIMEOUT, RunOutcome.PROVIDER_ERROR),
    )


def test_precedence_keeps_provider_error_over_a_concurrent_process_error() -> None:
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(outcome=RunOutcome.PROCESS_ERROR, return_code=1),
        provider_diagnostic=_provider_diagnostic(),
    )
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=agent_result,
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.PROVIDER_ERROR,
        concurrent_signals=(RunOutcome.PROVIDER_ERROR, RunOutcome.PROCESS_ERROR),
    )


def test_precedence_never_reads_terminal_response_once_timeout_forbids_it() -> None:
    """Even if a `terminal_response` is (adversarially) present on a timed
    out attempt, it must never surface as `SUCCEEDED` or otherwise change
    the outcome -- the parser's semantics are never consulted once a
    higher-precedence technical signal already applies."""

    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(
            outcome=RunOutcome.TIMEOUT, timed_out=True, return_code=None
        ),
        terminal_response=_success_response(AgentRole.CODER),
    )
    orchestrator, _run_store, _agent_runner, _git_safety = _orchestrator(
        call_log=[],
        sink=RecordingAttemptLogSink(),
        agent_result=agent_result,
        git_results=_safe_pair(),
        clock=SteppingClock(start=NOW),
    )

    result = orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="prompt",
        workspace=_workspace(),
    )

    assert result.precedence == AttemptPrecedence(
        outcome=RunOutcome.TIMEOUT, concurrent_signals=(RunOutcome.TIMEOUT,)
    )


# --- Typed result construction with orthogonal fields -----------------------


def test_logical_invocation_result_requires_matching_phase_for_role() -> None:
    mismatched_state = _pipeline_state(role=AgentRole.CODER, review_cycle=1)
    with pytest.raises(ValueError, match="state.phase must match role"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:ARCHITECT:0",
            role=AgentRole.ARCHITECT,
            state=mismatched_state,
            provider_attempt=1,
            git_before=_git_check(sequence=0, purpose="before"),
            agent_result=_agent_result(
                role=AgentRole.ARCHITECT, review_cycle=None, provider_attempt=1
            ),
            git_after=_git_check(sequence=1, purpose="after"),
            precedence=AttemptPrecedence(
                outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
            ),
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_a_blocked_result_to_have_no_agent_data() -> (
    None
):
    unsafe_before = _git_check(
        sequence=0, purpose="before", safety_status=GitSafetyStatus.UNSAFE
    )
    with pytest.raises(ValueError, match="agent_result must be None"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=unsafe_before,
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_after=None,
            precedence=None,
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_an_unblocked_result_to_have_agent_data() -> (
    None
):
    safe_before = _git_check(sequence=0, purpose="before")
    with pytest.raises(TypeError, match="agent_result must be AgentResult"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=safe_before,
            agent_result=None,
            git_after=None,
            precedence=None,
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_requires_agent_result_role_to_match() -> None:
    with pytest.raises(ValueError, match="agent_result role must match role"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=_git_check(sequence=0, purpose="before"),
            agent_result=_agent_result(
                role=AgentRole.REVIEWER, review_cycle=1, provider_attempt=1
            ),
            git_after=_git_check(sequence=1, purpose="after"),
            precedence=AttemptPrecedence(
                outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
            ),
            events=(
                InvocationEvent(
                    sequence=0,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_rejects_empty_events() -> None:
    with pytest.raises(ValueError, match="events must not be empty"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=_git_check(sequence=0, purpose="before"),
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_after=_git_check(sequence=1, purpose="after"),
            precedence=AttemptPrecedence(
                outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
            ),
            events=(),
        )


def test_logical_invocation_result_rejects_non_sequential_events() -> None:
    with pytest.raises(ValueError, match="sequential"):
        LogicalInvocationResult(
            logical_invocation_id="run-001:CODER:1",
            role=AgentRole.CODER,
            state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
            provider_attempt=1,
            git_before=_git_check(sequence=0, purpose="before"),
            agent_result=_agent_result(
                role=AgentRole.CODER, review_cycle=1, provider_attempt=1
            ),
            git_after=_git_check(sequence=1, purpose="after"),
            precedence=AttemptPrecedence(
                outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
            ),
            events=(
                InvocationEvent(
                    sequence=1,
                    kind=InvocationEventKind.INVOCATION_STARTED,
                    timestamp=NOW,
                ),
            ),
        )


def test_logical_invocation_result_keeps_git_and_technical_dimensions_orthogonal() -> (
    None
):
    """A technically successful attempt can still carry an UNSAFE `git_after`
    -- the two dimensions are reported side by side, never merged."""

    unsafe_after = _git_check(
        sequence=1, purpose="after", safety_status=GitSafetyStatus.UNSAFE
    )
    agent_result = _agent_result(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        terminal_response=_success_response(AgentRole.CODER),
    )

    result = LogicalInvocationResult(
        logical_invocation_id="run-001:CODER:1",
        role=AgentRole.CODER,
        state=_pipeline_state(role=AgentRole.CODER, review_cycle=1),
        provider_attempt=1,
        git_before=_git_check(sequence=0, purpose="before"),
        agent_result=agent_result,
        git_after=unsafe_after,
        precedence=AttemptPrecedence(
            outcome=RunOutcome.SUCCEEDED, concurrent_signals=(RunOutcome.SUCCEEDED,)
        ),
        events=(
            InvocationEvent(
                sequence=0, kind=InvocationEventKind.INVOCATION_STARTED, timestamp=NOW
            ),
        ),
    )

    assert result.precedence is not None
    assert result.precedence.outcome is RunOutcome.SUCCEEDED
    assert result.git_after is not None
    assert result.git_after.safety_status is GitSafetyStatus.UNSAFE

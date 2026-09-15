"""Component tests for `orchestrator.bootstrap_run` (M13-01).

Drives `bootstrap_run` with deterministic port fakes -- a scripted
`GitSafetyPort`, `IssueResolver`, `RunStorePort`, `TargetLeaseFactory`, and
`OpenCodePreflightPort` -- plus real `tmp_path`-derived absolute paths for
`Workspace`/`RunRequest`, exactly as a composed run would see them. No
concrete adapter is ever constructed. Proves the binding bootstrap/preflight
order (System Design SS8.2 steps 3-6, plus OpenCode's own exact-version/
capability/effective-agent/control-plane preflight, verified exactly once,
last, via `OpenCodePreflightPort.verify`), that the lease is acquired before
the baseline and stays held through every later step -- OpenCode's own
preflight included -- never released or quarantined by `bootstrap_run`
itself, that a single `IssueLocator` is resolved, that a failure at any
stage never constructs an `IssueOrchestrator` (so no agent role can ever be
invoked), that a run directory already created gets a best-effort amended
failure record without letting a second, unrelated persistence fault mask
the original error, and -- reusing M12's already-built `IssueOrchestrator.
run_logical_invocation` unchanged -- that a technical failure on the
architect's very first *attempt* (a separate, later concern from OpenCode's
own once-per-run preflight) still never reaches the coder or reviewer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import pytest

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    AppConfig,
    ConfigSource,
    ExecutionConfig,
    FinalStatus,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    IssueLocator,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    ProviderRetryConfig,
    RepositoryIdentity,
    RunOutcome,
    RunRecord,
    RunRequest,
    TargetRepository,
    Workspace,
)
from opencode_tools.errors import (
    GitSafetyError,
    LoggingError,
    PreflightError,
    ProtocolError,
)
from opencode_tools.orchestrator import (
    _TERMINATION_UNCONFIRMED_QUARANTINE_REASON,
    BootstrapOutcome,
    IssueOrchestrator,
    bootstrap_run,
    finalize_run,
)
from opencode_tools.ports import AttemptLogSink, LogChannel

NOW = datetime(2026, 9, 15, 9, 0, 0, tzinfo=UTC)
RUN_ID = "20260915T090000.000000Z-abcdefabcdef"


class SteppingClock:
    """A `Clock` fake whose `now()` advances by one second on every call."""

    def __init__(self, *, start: datetime = NOW) -> None:
        self._next = start

    def now(self) -> datetime:
        current = self._next
        self._next = current + timedelta(seconds=1)
        return current

    def monotonic_ns(self) -> int:
        raise AssertionError("not exercised by bootstrap_run")


class RecordingSleeper:
    """A `Sleeper` fake that records requested delays without ever sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


class ScriptedGitSafetyPort:
    """A `GitSafetyPort` fake: scriptable per-method outcomes plus a queue
    of post-baseline checkpoints for a later `IssueOrchestrator` call.
    `order_log`, when given, records a tag per call so a test can assert
    this port's calls interleave correctly with every other port's own."""

    def __init__(
        self,
        *,
        runtime_location_error: PreflightError | None = None,
        target: TargetRepository | None = None,
        resolve_target_error: PreflightError | None = None,
        baseline_check: GitCheckRecord | None = None,
        baseline_error: PreflightError | None = None,
        subsequent_checks: list[GitCheckRecord] | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        self.runtime_location_calls: list[Path] = []
        self.resolve_target_calls: list[tuple[Workspace, Path]] = []
        self.check_calls: list[
            tuple[TargetRepository, int, str, AgentRole | None, GitState | None]
        ] = []
        self._runtime_location_error = runtime_location_error
        self._target = target
        self._resolve_target_error = resolve_target_error
        self._baseline_check = baseline_check
        self._baseline_error = baseline_error
        self._subsequent_checks = list(subsequent_checks or [])
        self._order_log = order_log

    def check_runtime_location(self, runtime_root: Path) -> None:
        if self._order_log is not None:
            self._order_log.append("check_runtime_location")
        self.runtime_location_calls.append(runtime_root)
        if self._runtime_location_error is not None:
            raise self._runtime_location_error

    def resolve_target(
        self, workspace: Workspace, target_root: Path
    ) -> TargetRepository:
        if self._order_log is not None:
            self._order_log.append("resolve_target")
        self.resolve_target_calls.append((workspace, target_root))
        if self._resolve_target_error is not None:
            raise self._resolve_target_error
        assert self._target is not None
        return self._target

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
        self.check_calls.append((target, sequence, purpose, role, baseline))
        if sequence == 0:
            if self._baseline_error is not None:
                raise self._baseline_error
            assert self._baseline_check is not None
            return self._baseline_check
        return self._subsequent_checks.pop(0)


class ScriptedIssueResolver:
    """An `IssueResolver` fake returning one scripted identity/locator."""

    def __init__(
        self,
        *,
        repository_identity: RepositoryIdentity | None = None,
        resolve_error: PreflightError | None = None,
        issue_locator: IssueLocator | None = None,
        locate_error: PreflightError | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        self.resolve_calls: list[TargetRepository] = []
        self.locate_calls: list[tuple[RepositoryIdentity, int]] = []
        self._repository_identity = repository_identity
        self._resolve_error = resolve_error
        self._issue_locator = issue_locator
        self._locate_error = locate_error
        self._order_log = order_log

    def resolve_repository(self, target: TargetRepository) -> RepositoryIdentity:
        if self._order_log is not None:
            self._order_log.append("resolve_repository")
        self.resolve_calls.append(target)
        if self._resolve_error is not None:
            raise self._resolve_error
        assert self._repository_identity is not None
        return self._repository_identity

    def locate_issue(
        self, repository_identity: RepositoryIdentity, issue_number: int
    ) -> IssueLocator:
        if self._order_log is not None:
            self._order_log.append("locate_issue")
        self.locate_calls.append((repository_identity, issue_number))
        if self._locate_error is not None:
            raise self._locate_error
        assert self._issue_locator is not None
        return self._issue_locator


class ScriptedOpenCodePreflightPort:
    """An `OpenCodePreflightPort` fake: scriptable `verify`/`recheck`
    outcomes, standing in for OpenCode's exact-version/capability/
    effective-agent/control-plane preflight (System Design SS10.4/SS18.2;
    ADR-005) without ever invoking a real OpenCode adapter. `recheck_errors`,
    when given, is consumed one entry per `recheck` call (`None` means that
    call succeeds); once exhausted, every further `recheck` call succeeds."""

    def __init__(
        self,
        *,
        digest: str = "control-plane-digest-abc123",
        verify_error: PreflightError | None = None,
        recheck_errors: list[ProtocolError | None] | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        self.verify_calls = 0
        self.recheck_calls: list[str] = []
        self._digest = digest
        self._verify_error = verify_error
        self._recheck_errors = (
            list(recheck_errors) if recheck_errors is not None else None
        )
        self._order_log = order_log

    def verify(self) -> str:
        self.verify_calls += 1
        if self._order_log is not None:
            self._order_log.append("opencode_verify")
        if self._verify_error is not None:
            raise self._verify_error
        return self._digest

    def recheck(self, expected_digest: str) -> None:
        self.recheck_calls.append(expected_digest)
        if self._order_log is not None:
            self._order_log.append("opencode_recheck")
        if self._recheck_errors:
            error = self._recheck_errors.pop(0)
            if error is not None:
                raise error


class RecordingTargetLease:
    """A `TargetLease` fake: records release/quarantine without OS locking."""

    def __init__(self) -> None:
        self.released = False
        self.quarantine_reasons: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.released = True

    def quarantine(self, reason: str) -> None:
        self.quarantine_reasons.append(reason)


class ScriptedTargetLeaseFactory:
    """A `TargetLeaseFactory` fake returning one scripted lease or error."""

    def __init__(
        self,
        *,
        lease: RecordingTargetLease | None = None,
        error: PreflightError | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        self.calls: list[tuple[Path, Path, str]] = []
        self._lease = lease
        self._error = error
        self._order_log = order_log

    def acquire(
        self, target_root: Path, runtime_root: Path, run_id: str
    ) -> RecordingTargetLease:
        if self._order_log is not None:
            self._order_log.append("lease_acquire")
        self.calls.append((target_root, runtime_root, run_id))
        if self._error is not None:
            raise self._error
        assert self._lease is not None
        return self._lease


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


class RecordingRunStorePort:
    """A `RunStorePort` fake: a real `tmp_path` run directory, scriptable
    `persist` results, and one fresh sink per attempt sink request."""

    def __init__(
        self,
        *,
        persist_results: list[PersistenceStatus] | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        self.initialize_calls: list[tuple[Workspace, str]] = []
        self.sink_calls: list[tuple[AgentRole, int | None, int]] = []
        self.opened_sinks: list[RecordingAttemptLogSink] = []
        self.persist_calls: list[RunRecord] = []
        self._persist_results = (
            list(persist_results) if persist_results is not None else None
        )
        self._order_log = order_log

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        if self._order_log is not None:
            self._order_log.append("run_store_initialize")
        self.initialize_calls.append((workspace, run_id))
        run_directory = workspace.root / ".opencode-tools" / "runs" / run_id
        run_directory.mkdir(parents=True)
        return run_directory

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


class ScriptedAgentRunner:
    """An `AgentRunner` fake returning one scripted result per call."""

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


def _run_request(
    *, workspace: Workspace, target_root: Path, issue_number: int = 48
) -> RunRequest:
    return RunRequest(
        issue_number=issue_number, workspace=workspace, target_root=target_root
    )


def _app_config(*, runtime_root: Path) -> AppConfig:
    return AppConfig(
        source=ConfigSource.DEFAULTS,
        execution=ExecutionConfig(
            opencode_timeout_seconds=120.0,
            utility_timeout_seconds=10.0,
            termination_grace_seconds=5.0,
            max_review_cycles=3,
        ),
        provider_retry=ProviderRetryConfig(
            max_attempts=3,
            initial_delay_seconds=1.0,
            multiplier=2.0,
            max_delay_seconds=30.0,
        ),
        runtime_root=runtime_root,
    )


def _git_state(*, target_root: Path, fingerprint: str = "fp-baseline") -> GitState:
    return GitState(
        root=target_root,
        branch="main",
        head="deadbeef",
        porcelain_summary="",
        staged=(),
        unstaged=(),
        untracked=(),
        fingerprint=fingerprint,
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


def _baseline_check(
    *, target_root: Path, safety_status: GitSafetyStatus = GitSafetyStatus.SAFE
) -> GitCheckRecord:
    state = (
        _git_state(target_root=target_root)
        if safety_status is GitSafetyStatus.SAFE
        else GitState(
            root=target_root,
            branch=None,
            head=None,
            porcelain_summary="",
            staged=(),
            unstaged=(),
            untracked=(),
            fingerprint=None,
        )
    )
    return GitCheckRecord(
        sequence=0,
        purpose="baseline",
        process_results=(_git_probe_result(target_root=target_root),),
        state=state,
        safety_status=safety_status,
    )


def _repository_identity() -> RepositoryIdentity:
    return RepositoryIdentity(
        host="github.com",
        owner="manueldellapa",
        repository="OpenCode-Tools",
        source="origin",
    )


def _issue_locator(*, issue_number: int = 48) -> IssueLocator:
    return IssueLocator(repository_identity=_repository_identity(), number=issue_number)


def test_bootstrap_succeeds_in_the_binding_order_and_never_touches_the_lease_afterward(
    tmp_path: Path,
) -> None:
    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    order_log: list[str] = []
    git_safety = ScriptedGitSafetyPort(
        target=target,
        baseline_check=_baseline_check(target_root=target.root),
        order_log=order_log,
    )
    issue_resolver = ScriptedIssueResolver(
        repository_identity=_repository_identity(),
        issue_locator=_issue_locator(),
        order_log=order_log,
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease, order_log=order_log)
    run_store = RecordingRunStorePort(order_log=order_log)
    opencode_preflight = ScriptedOpenCodePreflightPort(order_log=order_log)
    agent_runner = ScriptedAgentRunner([])

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=opencode_preflight,
        agent_runner=agent_runner,
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert isinstance(outcome, BootstrapOutcome)
    assert outcome.error is None
    assert isinstance(outcome.orchestrator, IssueOrchestrator)
    assert outcome.issue_locator == _issue_locator()
    assert outcome.lease is lease

    # The full binding order (System Design SS8.2 steps 3-6, plus the
    # OpenCode preflight this issue restores): runtime location -> target
    # resolution -> lease acquisition (before the baseline) -> run
    # directory + first persist -> Git baseline -> GitHub identity/issue
    # resolution -> the OpenCode preflight, exactly once, last -> a second
    # persist.
    assert order_log == [
        "check_runtime_location",
        "resolve_target",
        "lease_acquire",
        "run_store_initialize",
        "persist:PREFLIGHT",
        "git_check:baseline",
        "resolve_repository",
        "locate_issue",
        "opencode_verify",
        "persist:ARCHITECT",
    ]
    assert opencode_preflight.verify_calls == 1

    assert git_safety.runtime_location_calls == [config.runtime_root]
    assert git_safety.resolve_target_calls == [(workspace, target.root)]
    assert lease_factory.calls == [(target.root, config.runtime_root, RUN_ID)]
    assert run_store.initialize_calls == [(workspace, RUN_ID)]
    assert len(run_store.persist_calls) == 2
    assert run_store.persist_calls[0].current_phase is PipelinePhase.PREFLIGHT
    assert run_store.persist_calls[0].git_baseline is None
    assert run_store.persist_calls[0].issue_locator is None
    assert git_safety.check_calls[0][1:4] == (0, "baseline", None)
    assert issue_resolver.resolve_calls == [target]
    assert issue_resolver.locate_calls == [
        (_repository_identity(), run_request.issue_number)
    ]

    ready_record = run_store.persist_calls[1]
    assert ready_record.current_phase is PipelinePhase.ARCHITECT
    assert ready_record.git_baseline == _git_state(target_root=target.root)
    assert ready_record.git_checks == (_baseline_check(target_root=target.root),)
    assert ready_record.issue_locator == _issue_locator()
    assert outcome.record == ready_record

    # The lease is handed back untouched: bootstrap_run never releases or
    # quarantines it, whatever the outcome (System Design SS16.2).
    assert lease.released is False
    assert lease.quarantine_reasons == []


def test_an_ambiguous_control_plane_fails_closed_and_converges_to_finalization(
    tmp_path: Path,
) -> None:
    """OpenCode's own exact-version/capability/effective-agent/control-plane
    preflight (System Design SS10.4; ADR-005) is run last, exactly once,
    after every other bootstrap/preflight fact -- and a failure there
    (standing in for an ambiguous or rejected control plane, e.g. an agent
    identity mismatch) must fail closed into the same PREFLIGHT-phase
    terminal path as every other preflight step, never invoke any agent
    role, and -- since a run directory already exists -- converge through
    `finalize_run` (M13-04), which alone releases the already-held lease
    only once finalization itself completes."""

    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    error = PreflightError(
        "opencode.debug_agent_identity_mismatch",
        "opencode debug agent coder did not identify itself as the requested role.",
    )
    postflight_check = GitCheckRecord(
        sequence=1,
        purpose="postflight",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    git_safety = ScriptedGitSafetyPort(
        target=target,
        baseline_check=_baseline_check(target_root=target.root),
        subsequent_checks=[postflight_check],
    )
    issue_resolver = ScriptedIssueResolver(
        repository_identity=_repository_identity(), issue_locator=_issue_locator()
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort()
    opencode_preflight = ScriptedOpenCodePreflightPort(verify_error=error)
    agent_runner = ScriptedAgentRunner([])

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=opencode_preflight,
        agent_runner=agent_runner,
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert outcome.error is error
    assert outcome.orchestrator is None  # no IssueOrchestrator is ever built
    assert outcome.issue_locator is None
    assert opencode_preflight.verify_calls == 1  # exactly once, never retried

    # No agent role was ever invoked -- there is no AgentRunner call to make
    # in bootstrap_run at all, and none was scripted to accept one either.
    assert agent_runner.calls == []

    # Every fact already gathered before the control-plane check survives
    # into the failure record, and the run enters the same common terminal
    # path (PREFLIGHT -> POSTFLIGHT) as any other preflight failure.
    assert len(run_store.persist_calls) == 3
    failure_record = run_store.persist_calls[1]
    assert failure_record.current_phase is PipelinePhase.POSTFLIGHT
    assert failure_record.errors[0].code == "opencode.debug_agent_identity_mismatch"
    assert failure_record.errors[0].outcome is RunOutcome.PREFLIGHT_ERROR
    assert failure_record.git_baseline == _git_state(target_root=target.root)
    assert failure_record.issue_locator == _issue_locator()
    assert outcome.record == failure_record

    # finalize_run re-checks Git once more against that same baseline, finds
    # no drift, persists the fully-resolved final record, and only then
    # releases the lease -- never before, and never left to a caller.
    final_record = run_store.persist_calls[2]
    assert final_record.current_phase is PipelinePhase.FINISHED
    assert final_record.git_postflight == postflight_check
    assert final_record.git_safety_status is GitSafetyStatus.SAFE
    assert final_record.final_status is FinalStatus.FAILED
    assert final_record.trigger_outcome is RunOutcome.PREFLIGHT_ERROR
    assert outcome.issue_result is not None
    assert outcome.issue_result.final_status is FinalStatus.FAILED
    assert outcome.issue_result.trigger_outcome is RunOutcome.PREFLIGHT_ERROR
    assert outcome.issue_result.expected_exit_code == 10

    assert outcome.lease is lease
    assert lease.released is True
    assert lease.quarantine_reasons == []


def test_runtime_location_failure_never_acquires_a_lease_or_touches_run_store(
    tmp_path: Path,
) -> None:
    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    error = PreflightError(
        "git_safety.runtime_root_under_git_metadata", "runtime root under .git"
    )
    git_safety = ScriptedGitSafetyPort(runtime_location_error=error, target=target)
    issue_resolver = ScriptedIssueResolver()
    lease_factory = ScriptedTargetLeaseFactory(lease=RecordingTargetLease())
    run_store = RecordingRunStorePort()

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=ScriptedAgentRunner([]),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert outcome.error is error
    assert outcome.orchestrator is None
    assert outcome.issue_locator is None
    assert outcome.lease is None
    assert outcome.record is None
    assert git_safety.resolve_target_calls == []
    assert lease_factory.calls == []
    assert run_store.initialize_calls == []
    assert run_store.persist_calls == []


def test_git_probe_failure_resolving_the_target_never_acquires_a_lease(
    tmp_path: Path,
) -> None:
    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    error = PreflightError("git_safety.dirty_worktree", "the target is dirty")
    git_safety = ScriptedGitSafetyPort(target=target, resolve_target_error=error)
    lease_factory = ScriptedTargetLeaseFactory(lease=RecordingTargetLease())
    run_store = RecordingRunStorePort()

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=ScriptedIssueResolver(),
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=ScriptedAgentRunner([]),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert outcome.error is error
    assert outcome.orchestrator is None
    assert outcome.lease is None
    assert lease_factory.calls == []
    assert run_store.initialize_calls == []


@pytest.mark.parametrize(
    "code",
    ["locking.target_locked", "locking.target_quarantined"],
)
def test_lock_contention_and_quarantine_fail_closed_before_run_store(
    tmp_path: Path, code: str
) -> None:
    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    error = PreflightError(code, "the target is unavailable")
    git_safety = ScriptedGitSafetyPort(target=target)
    lease_factory = ScriptedTargetLeaseFactory(error=error)
    run_store = RecordingRunStorePort()

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=ScriptedIssueResolver(),
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=ScriptedAgentRunner([]),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert outcome.error is error
    assert outcome.orchestrator is None
    assert outcome.lease is None
    assert lease_factory.calls == [(target.root, config.runtime_root, RUN_ID)]
    assert run_store.initialize_calls == []
    assert run_store.persist_calls == []


def test_a_first_persist_failure_blocks_everything_before_the_baseline(
    tmp_path: Path,
) -> None:
    """The very first `run.json` write fails, and so does the best-effort
    retry to persist an amended failure record -- a persistently broken
    store, not a transient one -- so no snapshot is ever durable and the
    baseline/GitHub steps are never reached."""

    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    git_safety = ScriptedGitSafetyPort(
        target=target, baseline_check=_baseline_check(target_root=target.root)
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort(
        persist_results=[
            PersistenceStatus.FAILED,
            PersistenceStatus.FAILED,
            PersistenceStatus.FAILED,
        ]
    )
    issue_resolver = ScriptedIssueResolver(
        repository_identity=_repository_identity(), issue_locator=_issue_locator()
    )

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=ScriptedAgentRunner([]),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert isinstance(outcome.error, LoggingError)
    assert outcome.orchestrator is None
    assert outcome.record is None  # never durably written, even the amend
    assert outcome.lease is lease  # already acquired
    # `latest_record` never had a baseline yet, so finalize_run's own
    # postflight probe is skipped entirely; its own persist attempt (the
    # third overall) is best-effort here too and also fails, so `record`
    # stays `None` -- but this terminal path still converges and releases.
    assert len(run_store.persist_calls) == 3
    assert git_safety.check_calls == []  # never reached the baseline
    assert issue_resolver.resolve_calls == []
    assert outcome.issue_result is not None
    assert outcome.issue_result.final_status is FinalStatus.FAILED
    assert outcome.issue_result.persistence_status is PersistenceStatus.FAILED
    assert outcome.issue_result.run_id == RUN_ID
    assert lease.released is True


def test_a_transient_first_persist_failure_still_records_the_error_on_retry(
    tmp_path: Path,
) -> None:
    """The first `run.json` write fails, but the best-effort retry to
    persist an amended failure record succeeds -- proving `bootstrap_run`
    does not give up on recording the error just because the very first
    write attempt failed."""

    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    git_safety = ScriptedGitSafetyPort(
        target=target, baseline_check=_baseline_check(target_root=target.root)
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort(
        persist_results=[
            PersistenceStatus.FAILED,
            PersistenceStatus.OK,
            PersistenceStatus.OK,
        ]
    )

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=ScriptedIssueResolver(),
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=ScriptedAgentRunner([]),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert isinstance(outcome.error, LoggingError)
    assert outcome.record is not None
    assert outcome.record.current_phase is PipelinePhase.POSTFLIGHT
    assert len(outcome.record.errors) == 1
    assert outcome.record.errors[0].outcome is RunOutcome.LOGGING_ERROR
    assert git_safety.check_calls == []  # never reached the baseline

    # finalize_run converges this too (M13-04): no baseline was ever
    # captured, so its own postflight probe is skipped, but it still runs
    # the gate, persists once more (the third write overall), and releases
    # the lease.
    assert len(run_store.persist_calls) == 3
    assert outcome.issue_result is not None
    assert outcome.issue_result.final_status is FinalStatus.FAILED
    assert outcome.issue_result.persistence_status is PersistenceStatus.OK
    assert outcome.issue_result.trigger_outcome is RunOutcome.LOGGING_ERROR
    assert lease.released is True


def test_an_indeterminate_baseline_fails_closed_and_persists_the_failure(
    tmp_path: Path,
) -> None:
    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    git_safety = ScriptedGitSafetyPort(
        target=target,
        baseline_check=_baseline_check(
            target_root=target.root, safety_status=GitSafetyStatus.INDETERMINATE
        ),
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort()
    issue_resolver = ScriptedIssueResolver()

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=ScriptedAgentRunner([]),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert isinstance(outcome.error, GitSafetyError)
    assert outcome.error.outcome is RunOutcome.GIT_SAFETY_ERROR
    assert outcome.orchestrator is None
    assert outcome.lease is lease
    assert issue_resolver.resolve_calls == []  # never reached GitHub identity

    # finalize_run converges this too (M13-04): the INDETERMINATE baseline
    # never became `git_baseline`, so its own postflight probe is skipped,
    # but it still runs the gate, persists once more, and releases the lease.
    assert len(run_store.persist_calls) == 3
    assert outcome.issue_result is not None
    assert outcome.issue_result.final_status is FinalStatus.FAILED
    assert outcome.issue_result.trigger_outcome is RunOutcome.GIT_SAFETY_ERROR
    assert outcome.issue_result.persistence_status is PersistenceStatus.OK
    assert lease.released is True

    failure_record = run_store.persist_calls[1]
    assert failure_record.current_phase is PipelinePhase.POSTFLIGHT
    assert len(failure_record.errors) == 1
    assert failure_record.errors[0].outcome is RunOutcome.GIT_SAFETY_ERROR
    assert outcome.record == failure_record

    # The INDETERMINATE checkpoint itself is preserved as audit evidence of
    # what was actually probed, even though it never becomes a trusted
    # baseline (System Design SS11.2/SS15.3): the "why" survives in
    # run.json alongside the error that names it.
    assert failure_record.git_checks == (
        _baseline_check(
            target_root=target.root, safety_status=GitSafetyStatus.INDETERMINATE
        ),
    )
    assert failure_record.git_baseline is None
    assert failure_record.git_safety_status is None


def test_a_github_identity_failure_fails_closed_and_persists_the_failure(
    tmp_path: Path,
) -> None:
    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    error = PreflightError("github.no_unique_identity", "ambiguous repository identity")
    postflight_check = GitCheckRecord(
        sequence=1,
        purpose="postflight",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    git_safety = ScriptedGitSafetyPort(
        target=target,
        baseline_check=_baseline_check(target_root=target.root),
        subsequent_checks=[postflight_check],
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort()
    issue_resolver = ScriptedIssueResolver(resolve_error=error)

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=ScriptedAgentRunner([]),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert outcome.error is error
    assert outcome.orchestrator is None
    assert outcome.lease is lease

    failure_record = run_store.persist_calls[1]
    assert failure_record.current_phase is PipelinePhase.POSTFLIGHT
    assert failure_record.errors[0].code == "github.no_unique_identity"
    assert outcome.record == failure_record

    # The Git baseline, already successfully captured before GitHub
    # identity resolution failed, is NOT discarded from the failure record
    # -- a later-stage failure must never lose an earlier stage's own,
    # already-durable-worthy evidence.
    assert failure_record.git_baseline == _git_state(target_root=target.root)
    assert failure_record.git_checks == (_baseline_check(target_root=target.root),)
    assert failure_record.git_safety_status is GitSafetyStatus.SAFE
    assert failure_record.issue_locator is None  # never reached: the failure IS here

    # finalize_run converges this too (M13-04): a baseline WAS captured, so
    # its own fresh postflight probe runs (role=CODER, against that same
    # baseline), finds no drift, persists once more, and releases the lease.
    assert len(run_store.persist_calls) == 3
    final_record = run_store.persist_calls[2]
    assert final_record.current_phase is PipelinePhase.FINISHED
    assert final_record.git_postflight == postflight_check
    assert outcome.issue_result is not None
    assert outcome.issue_result.final_status is FinalStatus.FAILED
    assert outcome.issue_result.trigger_outcome is RunOutcome.PREFLIGHT_ERROR
    assert outcome.issue_result.expected_exit_code == 10
    assert lease.released is True


def test_a_second_persist_failure_never_masks_the_original_preflight_error(
    tmp_path: Path,
) -> None:
    """A control-plane-level failure (here: an indeterminate baseline) that
    also can't be durably recorded (a second, unrelated persist fault) must
    still report the ORIGINAL error -- never the persistence fault -- while
    falling back to the last record that really did make it to disk (System
    Design SS15.4: the last valid `run.json` may stay partial)."""

    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    git_safety = ScriptedGitSafetyPort(
        target=target,
        baseline_check=_baseline_check(
            target_root=target.root, safety_status=GitSafetyStatus.INDETERMINATE
        ),
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort(
        persist_results=[
            PersistenceStatus.OK,
            PersistenceStatus.FAILED,
            PersistenceStatus.FAILED,
        ]
    )

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=ScriptedIssueResolver(),
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=ScriptedAgentRunner([]),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    assert isinstance(outcome.error, GitSafetyError)  # never LoggingError
    assert outcome.orchestrator is None
    assert outcome.lease is lease
    assert (
        len(run_store.persist_calls) == 3
    )  # the failed amend, then finalize_run's own
    assert outcome.record == run_store.persist_calls[0]  # fell back to the earlier one
    assert outcome.record is not None
    assert outcome.record.current_phase is PipelinePhase.PREFLIGHT

    # finalize_run's own final persist also fails on this persistently
    # broken store; its IssueResult falls back to that same earlier record
    # rather than ever reporting an unpersisted result, and the lease is
    # still released regardless.
    assert outcome.issue_result is not None
    assert outcome.issue_result.final_status is FinalStatus.FAILED
    assert outcome.issue_result.persistence_status is PersistenceStatus.FAILED
    assert outcome.issue_result.run_id == outcome.record.run_id
    assert outcome.issue_result.artifact_path == outcome.record.artifact_path
    assert lease.released is True


def test_a_control_plane_failure_on_the_architects_first_attempt_never_reaches_coder_or_reviewer(
    tmp_path: Path,
) -> None:
    """Bootstrap succeeds; the architect's own first attempt then fails with
    a technical, non-retryable outcome standing in for an OpenCode control-
    plane/compatibility failure. This reuses M12's already-built
    `IssueOrchestrator.run_logical_invocation` unchanged: `bootstrap_run`
    itself never invokes any agent role (System Design SS8.2's "nessun
    agent parte su preflight non verde"), and the composed pipeline must
    never advance to the coder or reviewer once the architect's own attempt
    fails this way."""

    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    architect_before = GitCheckRecord(
        sequence=1,
        purpose="ARCHITECT:0:1:before",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    architect_after = GitCheckRecord(
        sequence=2,
        purpose="ARCHITECT:0:1:after",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    git_safety = ScriptedGitSafetyPort(
        target=target,
        baseline_check=_baseline_check(target_root=target.root),
        subsequent_checks=[architect_before, architect_after],
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort()
    issue_resolver = ScriptedIssueResolver(
        repository_identity=_repository_identity(), issue_locator=_issue_locator()
    )
    control_plane_failure = AgentResult(
        role=AgentRole.ARCHITECT,
        phase=PipelinePhase.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        process=ProcessResult(
            command=("/usr/bin/opencode", "run"),
            cwd=workspace.root,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            duration_ns=1_000_000_000,
            return_code=None,
            timed_out=False,
            termination_confirmed=True,
            log_path=Path("architect-provider-attempt-1.log"),
            stdout_byte_count=0,
            stdout_sha256="stdout-digest",
            stderr_byte_count=0,
            stderr_sha256="stderr-digest",
            outcome=RunOutcome.PROCESS_ERROR,
        ),
        terminal_response=None,
        session_id=None,
        verified_agent=None,
        provider_diagnostic=None,
        outcome=RunOutcome.PROCESS_ERROR,
    )
    agent_runner = ScriptedAgentRunner([control_plane_failure])

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=agent_runner,
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )
    assert outcome.error is None
    assert isinstance(outcome.orchestrator, IssueOrchestrator)

    result = outcome.orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        prompt="Design the fix for issue 48.",
        workspace=workspace,
    )

    assert result.precedence is not None
    assert result.precedence.outcome is RunOutcome.PROCESS_ERROR
    assert result.retry_decision is not None
    assert result.retry_decision.should_retry is False
    # Exactly one attempt reached the agent runner -- the architect's own --
    # and no coder or reviewer invocation ever happened.
    assert [call[0] for call in agent_runner.calls] == [AgentRole.ARCHITECT]

    # The lease is still held: this composed failure is a normal terminal
    # attempt outcome, not something bootstrap_run (or this call) releases.
    assert lease.released is False


def test_a_control_plane_digest_drift_blocks_the_attempt_before_the_agent_runs(
    tmp_path: Path,
) -> None:
    """A control-plane digest drift caught by `OpenCodePreflightPort.
    recheck` (System Design SS18.2; ADR-005) blocks the attempt entirely,
    *before* the agent ever runs -- distinct from a technical failure *of*
    an attempt that did run (the `PROCESS_ERROR` case above). `run_logical_
    invocation` reports it via `control_plane_error`, never persists a
    placeholder `AttemptRecord` for it, and the composed pipeline never
    invokes the agent runner at all."""

    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    architect_before = GitCheckRecord(
        sequence=1,
        purpose="ARCHITECT:0:1:before",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    git_safety = ScriptedGitSafetyPort(
        target=target,
        baseline_check=_baseline_check(target_root=target.root),
        subsequent_checks=[architect_before],
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort()
    issue_resolver = ScriptedIssueResolver(
        repository_identity=_repository_identity(), issue_locator=_issue_locator()
    )
    drift_error = ProtocolError(
        "opencode.control_plane_drift",
        "The OpenCode control-plane digest changed since the initial preflight.",
    )
    opencode_preflight = ScriptedOpenCodePreflightPort(recheck_errors=[drift_error])
    agent_runner = ScriptedAgentRunner([])  # popping from this would raise IndexError

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=opencode_preflight,
        agent_runner=agent_runner,
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )
    assert outcome.error is None
    assert isinstance(outcome.orchestrator, IssueOrchestrator)
    persist_calls_before_invocation = len(run_store.persist_calls)

    result = outcome.orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        prompt="Design the fix for issue 48.",
        workspace=workspace,
    )

    assert result.control_plane_error is drift_error
    assert result.agent_result is None
    assert result.git_after is None
    assert result.precedence is None
    assert result.retry_decision is None
    assert agent_runner.calls == []  # the agent never ran at all
    assert len(opencode_preflight.recheck_calls) == 1

    # A blocked invocation is never persisted as a placeholder AttemptRecord.
    assert len(run_store.persist_calls) == persist_calls_before_invocation

    # The lease is still held: this is a normal terminal attempt outcome,
    # not something bootstrap_run (or this call) releases.
    assert lease.released is False


def test_ac_017_agent_reported_failure_is_distinct(tmp_path: Path) -> None:
    """AC-017: an agent-reported `FAILED` marker and a malformed/unparseable
    protocol envelope are two distinct, non-confusable technical outcomes
    (System Design SS13.2's precedence: `AGENT_REPORTED_FAILURE` vs.
    `PROTOCOL_ERROR`) -- driven here directly through this file's own
    ScriptedAgentRunner/bootstrap_run/IssueOrchestrator wiring (see
    `test_a_control_plane_failure_on_the_architects_first_attempt_never_reaches_coder_or_reviewer`
    for the pattern), mirroring the two proofs already in
    `test_single_issue_pipeline.py`'s `test_coder_is_not_invoked_when_the_
    architect_reports_failed` / `test_coder_is_not_invoked_when_the_architect_
    envelope_is_malformed` without importing that file's whole harness."""

    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    architect_before = GitCheckRecord(
        sequence=1,
        purpose="ARCHITECT:0:1:before",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    architect_after = GitCheckRecord(
        sequence=2,
        purpose="ARCHITECT:0:1:after",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    coder_before = GitCheckRecord(
        sequence=3,
        purpose="CODER:1:1:before",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    coder_after = GitCheckRecord(
        sequence=4,
        purpose="CODER:1:1:after",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    git_safety = ScriptedGitSafetyPort(
        target=target,
        baseline_check=_baseline_check(target_root=target.root),
        subsequent_checks=[
            architect_before,
            architect_after,
            coder_before,
            coder_after,
        ],
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort()
    issue_resolver = ScriptedIssueResolver(
        repository_identity=_repository_identity(), issue_locator=_issue_locator()
    )

    # (1) The architect ran, produced a real process outcome, and reported
    # its own role-specific FAILED marker with an explanation -- a
    # successful *parse* of an unsuccessful attempt (System Design SS9).
    architect_reported_failed = AgentResult(
        role=AgentRole.ARCHITECT,
        phase=PipelinePhase.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        process=ProcessResult(
            command=("/usr/bin/opencode", "run"),
            cwd=workspace.root,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            duration_ns=1_000_000_000,
            return_code=0,
            timed_out=False,
            termination_confirmed=True,
            log_path=Path("architect-provider-attempt-1.log"),
            stdout_byte_count=0,
            stdout_sha256="stdout-digest-1",
            stderr_byte_count=0,
            stderr_sha256="stderr-digest-1",
            outcome=RunOutcome.SUCCEEDED,
        ),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.ARCHITECT,
            body="Could not access the issue.",
            agent_status=AgentStatus.FAILED,
        ),
        session_id=None,
        verified_agent=None,
        provider_diagnostic=None,
        outcome=RunOutcome.AGENT_REPORTED_FAILURE,
    )
    # (2) The coder ran and its process outcome was clean too, but the
    # concrete adapter could not parse a terminal envelope out of it at all
    # (e.g. no marker line, or an inconsistent ISSUE_REF_JSON) -- reported
    # with `terminal_response=None`, exactly as if nothing had been said.
    coder_malformed_envelope = AgentResult(
        role=AgentRole.CODER,
        phase=PipelinePhase.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=ProcessResult(
            command=("/usr/bin/opencode", "run"),
            cwd=workspace.root,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            duration_ns=1_000_000_000,
            return_code=0,
            timed_out=False,
            termination_confirmed=True,
            log_path=Path("coder-provider-attempt-1.log"),
            stdout_byte_count=0,
            stdout_sha256="stdout-digest-2",
            stderr_byte_count=0,
            stderr_sha256="stderr-digest-2",
            outcome=RunOutcome.SUCCEEDED,
        ),
        terminal_response=None,
        session_id=None,
        verified_agent=None,
        provider_diagnostic=None,
        outcome=RunOutcome.PROTOCOL_ERROR,
    )
    agent_runner = ScriptedAgentRunner(
        [architect_reported_failed, coder_malformed_envelope]
    )

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=agent_runner,
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )
    assert outcome.error is None
    assert isinstance(outcome.orchestrator, IssueOrchestrator)

    failed_result = outcome.orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        prompt="Design the fix for issue 48.",
        workspace=workspace,
    )
    malformed_result = outcome.orchestrator.run_logical_invocation(
        role=AgentRole.CODER,
        review_cycle=1,
        provider_attempt=1,
        prompt="Implement the fix for issue 48.",
        workspace=workspace,
    )

    # The two halves of AC-017: distinct RunOutcomes, never confused.
    assert failed_result.precedence is not None
    assert failed_result.precedence.outcome is RunOutcome.AGENT_REPORTED_FAILURE
    assert malformed_result.precedence is not None
    assert malformed_result.precedence.outcome is RunOutcome.PROTOCOL_ERROR

    # Neither is ever retried (System Design SS13.2's precedence covers
    # both; only PROVIDER_ERROR retries).
    assert failed_result.retry_decision is not None
    assert failed_result.retry_decision.should_retry is False
    assert malformed_result.retry_decision is not None
    assert malformed_result.retry_decision.should_retry is False

    # This is a normal terminal attempt outcome, not something
    # bootstrap_run or run_logical_invocation itself releases.
    assert lease.released is False


def test_ac_020_all_terminal_failures_attempt_postflight(tmp_path: Path) -> None:
    """AC-020: every terminal-failure trigger attempts postflight and
    converges to `FINISHED`/`FinalStatus.FAILED` -- never skipped just
    because the trigger came from an agent attempt rather than a
    preflight-stage failure. Provider exhaustion and review-cycle
    exhaustion are already proven end to end in test_single_issue_pipeline.
    py (`test_finalize_run_reports_a_provider_exhaustion_trigger_as_failed`
    / `test_finalize_run_reports_review_cycles_exhausted_as_failed`); this
    covers the two trigger categories the PRD names that neither that file
    nor this one had ever chased through to `finalize_run` before: TIMEOUT,
    and a technical PROCESS_ERROR/PROTOCOL_ERROR (one grouped category in
    the PRD text -- both variants are covered below to remove ambiguity).
    `finalize_run`'s own logic never branches on which technical
    `RunOutcome` triggered it (it only branches on Git-safety/termination/
    persistence signals), so this exercises the same PREFLIGHT-stage
    convergence `test_an_ambiguous_control_plane_fails_closed_and_
    converges_to_finalization` already proves above, but for a trigger that
    only exists after at least one agent attempt has actually run."""

    def _run_one_scenario(
        *,
        scenario_root: Path,
        build_agent_result: Callable[[Workspace], AgentResult],
        expected_trigger: RunOutcome,
    ) -> None:
        scenario_root.mkdir()
        workspace, target = _workspace_and_target(scenario_root)
        config = _app_config(runtime_root=scenario_root / "runtime")
        run_request = _run_request(workspace=workspace, target_root=target.root)

        architect_before = GitCheckRecord(
            sequence=1,
            purpose="ARCHITECT:0:1:before",
            process_results=(_git_probe_result(target_root=target.root),),
            state=_git_state(target_root=target.root),
            safety_status=GitSafetyStatus.SAFE,
        )
        architect_after = GitCheckRecord(
            sequence=2,
            purpose="ARCHITECT:0:1:after",
            process_results=(_git_probe_result(target_root=target.root),),
            state=_git_state(target_root=target.root),
            safety_status=GitSafetyStatus.SAFE,
        )
        postflight_check = GitCheckRecord(
            sequence=3,
            purpose="postflight",
            process_results=(_git_probe_result(target_root=target.root),),
            state=_git_state(target_root=target.root),
            safety_status=GitSafetyStatus.SAFE,
        )
        git_safety = ScriptedGitSafetyPort(
            target=target,
            baseline_check=_baseline_check(target_root=target.root),
            subsequent_checks=[architect_before, architect_after, postflight_check],
        )
        lease = RecordingTargetLease()
        lease_factory = ScriptedTargetLeaseFactory(lease=lease)
        run_store = RecordingRunStorePort()
        issue_resolver = ScriptedIssueResolver(
            repository_identity=_repository_identity(), issue_locator=_issue_locator()
        )
        # `workspace` (needed for `ProcessResult.cwd`) is only known once
        # `_workspace_and_target` has run, so the one scripted result is
        # built here, right before bootstrap_run, rather than up front.
        agent_runner = ScriptedAgentRunner([build_agent_result(workspace)])

        outcome = bootstrap_run(
            run_request=run_request,
            config=config,
            run_id=RUN_ID,
            config_snapshot={},
            environment_snapshot={},
            git_safety=git_safety,
            issue_resolver=issue_resolver,
            run_store=run_store,
            lease_factory=lease_factory,
            opencode_preflight=ScriptedOpenCodePreflightPort(),
            agent_runner=agent_runner,
            clock=SteppingClock(),
            sleeper=RecordingSleeper(),
        )
        assert outcome.error is None
        assert isinstance(outcome.orchestrator, IssueOrchestrator)

        result = outcome.orchestrator.run_logical_invocation(
            role=AgentRole.ARCHITECT,
            review_cycle=None,
            provider_attempt=1,
            prompt="Design the fix for issue 48.",
            workspace=workspace,
        )
        assert result.precedence is not None
        assert result.precedence.outcome is expected_trigger
        assert result.agent_result is not None

        issue_result = finalize_run(
            record=outcome.orchestrator.record,
            target=target,
            trigger_outcome=result.precedence.outcome,
            review_status=None,
            interrupted=False,
            termination_confirmed=result.agent_result.process.termination_confirmed,
            git_safety=git_safety,
            run_store=run_store,
            lease=lease,
            clock=SteppingClock(),
            max_review_cycles=config.execution.max_review_cycles,
        )

        # The postflight probe WAS attempted -- the whole point of AC-020 --
        # never skipped just because the trigger was technical rather than
        # a preflight-stage failure.
        assert git_safety.check_calls[-1][2] == "postflight"
        final_record = run_store.persist_calls[-1]
        assert final_record.current_phase is PipelinePhase.FINISHED
        assert final_record.git_postflight == postflight_check
        assert issue_result.final_status is FinalStatus.FAILED
        assert issue_result.trigger_outcome is expected_trigger
        assert issue_result.expected_exit_code == 20
        assert lease.released is True

    def _timeout_result(workspace: Workspace) -> AgentResult:
        return AgentResult(
            role=AgentRole.ARCHITECT,
            phase=PipelinePhase.ARCHITECT,
            review_cycle=None,
            provider_attempt=1,
            process=ProcessResult(
                command=("/usr/bin/opencode", "run"),
                cwd=workspace.root,
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=120),
                duration_ns=120_000_000_000,
                return_code=None,
                timed_out=True,
                termination_confirmed=True,
                log_path=Path("architect-provider-attempt-1.log"),
                stdout_byte_count=0,
                stdout_sha256="stdout-digest",
                stderr_byte_count=0,
                stderr_sha256="stderr-digest",
                outcome=RunOutcome.TIMEOUT,
            ),
            terminal_response=None,
            session_id=None,
            verified_agent=None,
            provider_diagnostic=None,
            outcome=RunOutcome.TIMEOUT,
        )

    def _process_error_result(workspace: Workspace) -> AgentResult:
        return AgentResult(
            role=AgentRole.ARCHITECT,
            phase=PipelinePhase.ARCHITECT,
            review_cycle=None,
            provider_attempt=1,
            process=ProcessResult(
                command=("/usr/bin/opencode", "run"),
                cwd=workspace.root,
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=1),
                duration_ns=1_000_000_000,
                return_code=None,
                timed_out=False,
                termination_confirmed=True,
                log_path=Path("architect-provider-attempt-1.log"),
                stdout_byte_count=0,
                stdout_sha256="stdout-digest",
                stderr_byte_count=0,
                stderr_sha256="stderr-digest",
                outcome=RunOutcome.PROCESS_ERROR,
            ),
            terminal_response=None,
            session_id=None,
            verified_agent=None,
            provider_diagnostic=None,
            outcome=RunOutcome.PROCESS_ERROR,
        )

    def _protocol_error_result(workspace: Workspace) -> AgentResult:
        return AgentResult(
            role=AgentRole.ARCHITECT,
            phase=PipelinePhase.ARCHITECT,
            review_cycle=None,
            provider_attempt=1,
            process=ProcessResult(
                command=("/usr/bin/opencode", "run"),
                cwd=workspace.root,
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=1),
                duration_ns=1_000_000_000,
                return_code=0,
                timed_out=False,
                termination_confirmed=True,
                log_path=Path("architect-provider-attempt-1.log"),
                stdout_byte_count=0,
                stdout_sha256="stdout-digest",
                stderr_byte_count=0,
                stderr_sha256="stderr-digest",
                outcome=RunOutcome.SUCCEEDED,
            ),
            terminal_response=None,
            session_id=None,
            verified_agent=None,
            provider_diagnostic=None,
            outcome=RunOutcome.PROTOCOL_ERROR,
        )

    _run_one_scenario(
        scenario_root=tmp_path / "timeout",
        build_agent_result=_timeout_result,
        expected_trigger=RunOutcome.TIMEOUT,
    )
    _run_one_scenario(
        scenario_root=tmp_path / "process-error",
        build_agent_result=_process_error_result,
        expected_trigger=RunOutcome.PROCESS_ERROR,
    )
    _run_one_scenario(
        scenario_root=tmp_path / "protocol-error",
        build_agent_result=_protocol_error_result,
        expected_trigger=RunOutcome.PROTOCOL_ERROR,
    )


def test_ac_032_unconfirmed_termination_quarantines_and_fails(tmp_path: Path) -> None:
    """AC-032: an attempt whose termination could not be confirmed forces
    `finalize_run`'s own postflight determination to `INDETERMINATE` even
    when the fresh probe itself comes back `SAFE`, quarantines the target
    *before* the lease is ever released, and still converges to a
    persisted `FinalStatus.FAILED` -- reusing this file's own bootstrap_run
    wiring (as in `test_a_control_plane_failure_on_the_architects_first_
    attempt_never_reaches_coder_or_reviewer`) to reach a valid, baselined
    `RunRecord`, then calling `finalize_run` directly with
    `termination_confirmed=False`. This is the same convergence
    `test_single_issue_pipeline.py`'s `test_finalize_run_quarantines_and_
    marks_indeterminate_when_termination_is_unconfirmed` already proves,
    reached here without importing that file's whole harness. The real,
    bounded (never-hanging) SIGTERM/grace/SIGKILL escalation that produces
    an unconfirmed termination in the first place is proven separately, at
    the real-subprocess level, by test_process_runner.py::test_run_reports_
    unconfirmed_termination_when_a_descendant_escapes_the_group and its
    grace-bound siblings -- this test only proves what `finalize_run` does
    once handed `termination_confirmed=False`, against a fake port."""

    workspace, target = _workspace_and_target(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")
    run_request = _run_request(workspace=workspace, target_root=target.root)

    postflight_check = GitCheckRecord(
        sequence=1,
        purpose="postflight",
        process_results=(_git_probe_result(target_root=target.root),),
        state=_git_state(target_root=target.root),
        safety_status=GitSafetyStatus.SAFE,
    )
    git_safety = ScriptedGitSafetyPort(
        target=target,
        baseline_check=_baseline_check(target_root=target.root),
        subsequent_checks=[postflight_check],
    )
    lease = RecordingTargetLease()
    lease_factory = ScriptedTargetLeaseFactory(lease=lease)
    run_store = RecordingRunStorePort()
    issue_resolver = ScriptedIssueResolver(
        repository_identity=_repository_identity(), issue_locator=_issue_locator()
    )

    outcome = bootstrap_run(
        run_request=run_request,
        config=config,
        run_id=RUN_ID,
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=ScriptedOpenCodePreflightPort(),
        agent_runner=ScriptedAgentRunner([]),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )
    assert outcome.error is None
    assert outcome.record is not None
    assert lease.quarantine_reasons == []  # nothing quarantined yet

    issue_result = finalize_run(
        record=outcome.record,
        target=target,
        trigger_outcome=RunOutcome.PROCESS_ERROR,
        review_status=None,
        interrupted=False,
        termination_confirmed=False,
        git_safety=git_safety,
        run_store=run_store,
        lease=lease,
        clock=SteppingClock(),
        max_review_cycles=config.execution.max_review_cycles,
    )

    # Quarantined with production's own reason -- before the lease is
    # released, never after -- not a different, test-invented string.
    assert lease.quarantine_reasons == [_TERMINATION_UNCONFIRMED_QUARANTINE_REASON]
    assert lease.released is True

    final_record = run_store.persist_calls[-1]
    assert final_record.current_phase is PipelinePhase.FINISHED
    # The raw probe evidence is preserved verbatim -- it really was SAFE --
    # even though it can never be TRUSTED once termination is unconfirmed.
    assert final_record.git_postflight == postflight_check
    assert final_record.git_postflight is not None
    assert final_record.git_postflight.safety_status is GitSafetyStatus.SAFE
    assert final_record.git_safety_status is GitSafetyStatus.INDETERMINATE
    assert final_record.termination_confirmed is False

    assert issue_result.final_status is FinalStatus.FAILED
    assert issue_result.git_safety_status is GitSafetyStatus.INDETERMINATE
    assert issue_result.termination_confirmed is False

"""Component tests for `orchestrator.bootstrap_run` across a multi-repository
workspace (M13-01; System Design AC-003).

A single workspace containing two independent Git repositories -- `Backend`
and `Frontend` -- each gets its own `bootstrap_run` call. Every port fake
here is keyed by target root rather than scripted with one fixed value, so
these tests prove `bootstrap_run` addresses each target explicitly and
never conflates the two: `GitSafetyPort.resolve_target`/`check` always see
the *matching* target, `IssueLocator` resolution is derived from each
target's own resolved GitHub identity, and `TargetLeaseFactory.acquire`
hands back a distinct lease per target so the two runs could proceed with
independent exclusion, exactly as System Design SS16.1/SS16.2 requires for
two different target repositories.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self, cast

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AppConfig,
    ConfigSource,
    ExecutionConfig,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    IssueLocator,
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
from opencode_tools.orchestrator import IssueOrchestrator, bootstrap_run
from opencode_tools.ports import AttemptLogSink, LogChannel

NOW = datetime(2026, 9, 15, 9, 0, 0, tzinfo=UTC)


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


class ScriptedAgentRunner:
    """An `AgentRunner` fake never expected to be called by `bootstrap_run`."""

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
        raise AssertionError("bootstrap_run never invokes an agent role")


class RecordingAgentRunner:
    """An `AgentRunner` fake that records every call and returns one
    scripted `AgentResult` -- unlike `ScriptedAgentRunner`, used only where a
    test drives a real logical invocation past bootstrap, to prove *which*
    value (`workspace`, never a target root) `IssueOrchestrator` hands it."""

    def __init__(self, result: AgentResult) -> None:
        self._result = result
        self.calls: list[tuple[AgentRole, Workspace, int | None, int]] = []

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
        self.calls.append((role, workspace, review_cycle, provider_attempt))
        return self._result


class RecordingOpenCodePreflightPort:
    """An `OpenCodePreflightPort` fake that always succeeds, recording how
    many times each independent `bootstrap_run` call verified/rechecked it."""

    def __init__(self, *, digest: str = "control-plane-digest-abc123") -> None:
        self.verify_calls = 0
        self.recheck_calls: list[str] = []
        self._digest = digest

    def verify(self) -> str:
        self.verify_calls += 1
        return self._digest

    def recheck(self, expected_digest: str) -> None:
        self.recheck_calls.append(expected_digest)


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
        stderr_sha256="stderr-digest",
        outcome=RunOutcome.SUCCEEDED,
    )


def _git_state(*, target_root: Path, fingerprint: str) -> GitState:
    return GitState(
        root=target_root,
        branch="main",
        head=f"head-{fingerprint}",
        porcelain_summary="",
        staged=(),
        unstaged=(),
        untracked=(),
        fingerprint=fingerprint,
    )


def _baseline_check(*, target_root: Path, fingerprint: str) -> GitCheckRecord:
    return GitCheckRecord(
        sequence=0,
        purpose="baseline",
        process_results=(_git_probe_result(target_root=target_root),),
        state=_git_state(target_root=target_root, fingerprint=fingerprint),
        safety_status=GitSafetyStatus.SAFE,
    )


def _architect_process_error_result(*, workspace_root: Path) -> AgentResult:
    """A minimal technical-failure `AgentResult` for the architect -- its
    content is irrelevant to this file's tests, which never inspect
    protocol precedence, only *which paths* the pipeline addressed."""

    return AgentResult(
        role=AgentRole.ARCHITECT,
        phase=PipelinePhase.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        process=ProcessResult(
            command=("/usr/bin/opencode", "run"),
            cwd=workspace_root,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            duration_ns=1_000_000_000,
            return_code=None,
            timed_out=False,
            termination_confirmed=True,
            log_path=Path("architect-attempt.log"),
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


class MultiTargetGitSafetyPort:
    """A `GitSafetyPort` fake keyed by target root -- proves `bootstrap_run`
    always addresses the *matching* target explicitly (System Design
    AC-003), never the other repository sharing the same workspace."""

    def __init__(self, *, targets: dict[Path, TargetRepository]) -> None:
        self._targets = targets
        self.runtime_location_calls: list[Path] = []
        self.resolve_target_calls: list[tuple[Workspace, Path]] = []
        self.check_calls: list[tuple[Path, int, str]] = []

    def check_runtime_location(self, runtime_root: Path) -> None:
        self.runtime_location_calls.append(runtime_root)

    def resolve_target(
        self, workspace: Workspace, target_root: Path
    ) -> TargetRepository:
        self.resolve_target_calls.append((workspace, target_root))
        return self._targets[target_root]

    def check(
        self,
        target: TargetRepository,
        *,
        sequence: int,
        purpose: str,
        role: AgentRole | None = None,
        baseline: GitState | None = None,
    ) -> GitCheckRecord:
        self.check_calls.append((target.root, sequence, purpose))
        assert target.root in self._targets
        return _baseline_check(target_root=target.root, fingerprint=target.root.name)


class MultiTargetIssueResolver:
    """An `IssueResolver` fake keyed by target root: each repository
    resolves to its own distinct GitHub identity (System Design SS17.1)."""

    def __init__(self, *, identities: dict[Path, RepositoryIdentity]) -> None:
        self._identities = identities
        self.resolve_calls: list[Path] = []
        self.locate_calls: list[tuple[RepositoryIdentity, int]] = []

    def resolve_repository(self, target: TargetRepository) -> RepositoryIdentity:
        self.resolve_calls.append(target.root)
        return self._identities[target.root]

    def locate_issue(
        self, repository_identity: RepositoryIdentity, issue_number: int
    ) -> IssueLocator:
        self.locate_calls.append((repository_identity, issue_number))
        return IssueLocator(
            repository_identity=repository_identity, number=issue_number
        )


class RecordingTargetLease:
    """A `TargetLease` fake that records release/quarantine, tagged by the
    target it was acquired for so a test can tell two leases apart."""

    def __init__(self, *, target_root: Path) -> None:
        self.target_root = target_root
        self.released = False
        self.quarantine_reasons: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.released = True

    def quarantine(self, reason: str) -> None:
        self.quarantine_reasons.append(reason)


class MultiTargetLeaseFactory:
    """A `TargetLeaseFactory` fake that hands back one distinct
    `RecordingTargetLease` per target root, mirroring
    `PosixTargetLeaseFactory`'s real key (the target's own absolute Git
    directory, never `runtime_root`) so two different repositories in the
    same workspace never collide on one lease (System Design SS16.1/16.2)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path, str]] = []
        self._leases: dict[Path, RecordingTargetLease] = {}

    def acquire(
        self, target_root: Path, runtime_root: Path, run_id: str
    ) -> RecordingTargetLease:
        self.calls.append((target_root, runtime_root, run_id))
        lease = RecordingTargetLease(target_root=target_root)
        self._leases[target_root] = lease
        return lease


class RecordingAttemptLogSink:
    """A minimal `AttemptLogSink` fake; never opened by `bootstrap_run`."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        raise AssertionError("bootstrap_run never opens an attempt sink")

    def close(self) -> None:
        raise AssertionError("bootstrap_run never opens an attempt sink")


class _NullAttemptLogSink:
    """A no-op `AttemptLogSink`, only ever handed out when a test actually
    drives a logical invocation past bootstrap (`allow_attempt_sink=True`)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        return None

    def close(self) -> None:
        return None


class RecordingRunStorePort:
    """A `RunStorePort` fake sharing one real `tmp_path` workspace across
    multiple, independently keyed runs (one per target). `open_attempt_sink`
    raises by default -- `bootstrap_run` itself never opens one -- unless
    `allow_attempt_sink=True`, needed only by a test that drives a real
    logical invocation past bootstrap."""

    def __init__(self, *, allow_attempt_sink: bool = False) -> None:
        self.initialize_calls: list[tuple[Workspace, str]] = []
        self.persist_calls: list[RunRecord] = []
        self._allow_attempt_sink = allow_attempt_sink

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        self.initialize_calls.append((workspace, run_id))
        run_directory = workspace.root / ".opencode-tools" / "runs" / run_id
        run_directory.mkdir(parents=True)
        return run_directory

    def open_attempt_sink(
        self, role: AgentRole, review_cycle: int | None, provider_attempt: int
    ) -> AttemptLogSink:
        if not self._allow_attempt_sink:
            raise AssertionError("bootstrap_run never opens an attempt sink")
        return _NullAttemptLogSink(Path(f"{role.value.lower()}-attempt.log"))

    def persist(self, record: RunRecord) -> PersistenceStatus:
        self.persist_calls.append(record)
        return PersistenceStatus.OK

    def stage_final(self, record: RunRecord) -> PersistenceStatus:
        raise AssertionError("finalization is not exercised by these tests")

    def commit_final(self) -> PersistenceStatus:
        raise AssertionError("finalization is not exercised by these tests")

    def abort_final(self) -> None:
        return None


def _multirepo_workspace(
    tmp_path: Path,
) -> tuple[Workspace, TargetRepository, TargetRepository]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace = Workspace(root=workspace_root)

    backend_root = workspace_root / "Backend"
    backend_root.mkdir()
    backend = TargetRepository(
        root=backend_root,
        workspace_relative=Path("Backend"),
        git_common_dir=backend_root / ".git",
    )

    frontend_root = workspace_root / "Frontend"
    frontend_root.mkdir()
    frontend = TargetRepository(
        root=frontend_root,
        workspace_relative=Path("Frontend"),
        git_common_dir=frontend_root / ".git",
    )
    return workspace, backend, frontend


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


def test_two_targets_in_one_workspace_bootstrap_independently_without_conflating_them(
    tmp_path: Path,
) -> None:
    workspace, backend, frontend = _multirepo_workspace(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")

    git_safety = MultiTargetGitSafetyPort(
        targets={backend.root: backend, frontend.root: frontend}
    )
    issue_resolver = MultiTargetIssueResolver(
        identities={
            backend.root: RepositoryIdentity(
                host="github.com", owner="acme", repository="Backend", source="origin"
            ),
            frontend.root: RepositoryIdentity(
                host="github.com", owner="acme", repository="Frontend", source="origin"
            ),
        }
    )
    lease_factory = MultiTargetLeaseFactory()
    run_store = RecordingRunStorePort()
    opencode_preflight = RecordingOpenCodePreflightPort()

    backend_outcome = bootstrap_run(
        run_request=RunRequest(
            issue_number=21, workspace=workspace, target_root=backend.root
        ),
        config=config,
        run_id="20260915T090000.000000Z-000000000001",
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=opencode_preflight,
        agent_runner=ScriptedAgentRunner(),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )
    frontend_outcome = bootstrap_run(
        run_request=RunRequest(
            issue_number=22, workspace=workspace, target_root=frontend.root
        ),
        config=config,
        run_id="20260915T090000.000000Z-000000000002",
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=opencode_preflight,
        agent_runner=ScriptedAgentRunner(),
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )

    # OpenCode's own preflight verified exactly once per run -- twice total
    # for the two independent runs in this workspace, never conflated.
    assert opencode_preflight.verify_calls == 2

    for outcome in (backend_outcome, frontend_outcome):
        assert outcome.error is None
        assert isinstance(outcome.orchestrator, IssueOrchestrator)

    # Each run resolved against its OWN target -- Backend never saw
    # Frontend's root and vice versa (System Design AC-003).
    assert git_safety.resolve_target_calls == [
        (workspace, backend.root),
        (workspace, frontend.root),
    ]
    assert {root for root, _seq, _purpose in git_safety.check_calls} == {
        backend.root,
        frontend.root,
    }

    # Each run got its own distinct lease, keyed by its own target root --
    # never one lease shared or conflated across the two repositories.
    assert lease_factory.calls == [
        (backend.root, config.runtime_root, "20260915T090000.000000Z-000000000001"),
        (frontend.root, config.runtime_root, "20260915T090000.000000Z-000000000002"),
    ]
    assert backend_outcome.lease is not frontend_outcome.lease
    assert backend_outcome.lease is not None
    assert frontend_outcome.lease is not None
    backend_lease = cast(RecordingTargetLease, backend_outcome.lease)
    frontend_lease = cast(RecordingTargetLease, frontend_outcome.lease)
    assert backend_lease.target_root == backend.root
    assert frontend_lease.target_root == frontend.root

    # Each run resolved its own IssueLocator against its own GitHub identity.
    assert backend_outcome.issue_locator is not None
    assert backend_outcome.issue_locator.repository_identity.repository == "Backend"
    assert backend_outcome.issue_locator.number == 21
    assert frontend_outcome.issue_locator is not None
    assert frontend_outcome.issue_locator.repository_identity.repository == "Frontend"
    assert frontend_outcome.issue_locator.number == 22

    # Each run got its own run directory and persisted RunRecord, whose
    # `target` reflects its own repository, never the other one's.
    assert len(run_store.persist_calls) == 4  # two persists (bootstrap + ready) each
    backend_ready = run_store.persist_calls[1]
    frontend_ready = run_store.persist_calls[3]
    assert backend_ready.target == backend
    assert backend_ready.issue_number == 21
    assert frontend_ready.target == frontend
    assert frontend_ready.issue_number == 22

    # Both leases remain held: bootstrap_run releases neither.
    assert backend_lease.released is False
    assert frontend_lease.released is False


def test_current_phase_reaches_architect_independently_for_each_target(
    tmp_path: Path,
) -> None:
    workspace, backend, frontend = _multirepo_workspace(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")

    git_safety = MultiTargetGitSafetyPort(
        targets={backend.root: backend, frontend.root: frontend}
    )
    issue_resolver = MultiTargetIssueResolver(
        identities={
            backend.root: RepositoryIdentity(
                host="github.com", owner="acme", repository="Backend", source="origin"
            ),
            frontend.root: RepositoryIdentity(
                host="github.com", owner="acme", repository="Frontend", source="origin"
            ),
        }
    )
    lease_factory = MultiTargetLeaseFactory()
    run_store = RecordingRunStorePort()

    for index, target in enumerate((backend, frontend), start=1):
        outcome = bootstrap_run(
            run_request=RunRequest(
                issue_number=index, workspace=workspace, target_root=target.root
            ),
            config=config,
            run_id=f"20260915T090000.00000{index}Z-00000000000{index}",
            config_snapshot={},
            environment_snapshot={},
            git_safety=git_safety,
            issue_resolver=issue_resolver,
            run_store=run_store,
            lease_factory=lease_factory,
            opencode_preflight=RecordingOpenCodePreflightPort(),
            agent_runner=ScriptedAgentRunner(),
            clock=SteppingClock(),
            sleeper=RecordingSleeper(),
        )
        assert outcome.record is not None
        assert outcome.record.current_phase is PipelinePhase.ARCHITECT
        assert outcome.record.target == target


# --- AC-003: workspace/target split ------------------------------------------


def test_ac_003_workspace_target_split(
    tmp_path: Path,
) -> None:
    """Bootstrapping alone only proves *resolution* stays distinct per
    target (the tests above); once a role actually runs, `IssueOrchestrator`
    must still hand `AgentRunner.run` the *workspace* -- OpenCode always
    executes there -- while every `GitSafetyPort.check` call keeps
    addressing the *specific* target repository, and Frontend, sharing the
    same workspace, must stay completely untouched by a Backend-only
    invocation (System Design AC-003: "OpenCode nel workspace, Git/diff sul
    target esplicito")."""

    workspace, backend, frontend = _multirepo_workspace(tmp_path)
    config = _app_config(runtime_root=tmp_path / "runtime")

    git_safety = MultiTargetGitSafetyPort(
        targets={backend.root: backend, frontend.root: frontend}
    )
    issue_resolver = MultiTargetIssueResolver(
        identities={
            backend.root: RepositoryIdentity(
                host="github.com", owner="acme", repository="Backend", source="origin"
            ),
            frontend.root: RepositoryIdentity(
                host="github.com", owner="acme", repository="Frontend", source="origin"
            ),
        }
    )
    lease_factory = MultiTargetLeaseFactory()
    run_store = RecordingRunStorePort(allow_attempt_sink=True)
    agent_runner = RecordingAgentRunner(
        result=_architect_process_error_result(workspace_root=workspace.root)
    )

    backend_outcome = bootstrap_run(
        run_request=RunRequest(
            issue_number=21, workspace=workspace, target_root=backend.root
        ),
        config=config,
        run_id="20260915T090000.000000Z-000000000001",
        config_snapshot={},
        environment_snapshot={},
        git_safety=git_safety,
        issue_resolver=issue_resolver,
        run_store=run_store,
        lease_factory=lease_factory,
        opencode_preflight=RecordingOpenCodePreflightPort(),
        agent_runner=agent_runner,
        clock=SteppingClock(),
        sleeper=RecordingSleeper(),
    )
    assert backend_outcome.error is None
    assert isinstance(backend_outcome.orchestrator, IssueOrchestrator)
    git_check_count_before_invocation = len(git_safety.check_calls)

    backend_outcome.orchestrator.run_logical_invocation(
        role=AgentRole.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        prompt="Design the fix for issue 21.",
        workspace=workspace,
    )

    # OpenCode ran against the shared *workspace* -- never Backend's own root.
    assert agent_runner.calls == [(AgentRole.ARCHITECT, workspace, None, 1)]

    # Every Git check this invocation made addressed Backend explicitly --
    # Frontend, sharing the same workspace, was never touched by it.
    invocation_checks = git_safety.check_calls[git_check_count_before_invocation:]
    assert invocation_checks  # at least the before/after checkpoints ran
    assert {root for root, _seq, _purpose in invocation_checks} == {backend.root}

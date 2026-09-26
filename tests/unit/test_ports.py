"""Unit tests for the substitutable application `Protocol` boundaries.

Each test builds a minimal fake or recording implementation, exercises it
through a variable annotated with the port's `Protocol` type -- so mypy
strict checks structural conformance -- and asserts on the concrete fake to
verify the essential signature was honored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    IssueLocator,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    ProcessSpec,
    RepositoryIdentity,
    RunOutcome,
    RunRecord,
    TargetRepository,
    Workspace,
)
from opencode_tools.errors import PreflightError, ProtocolError
from opencode_tools.ports import (
    AgentRunner,
    AttemptLogSink,
    Clock,
    GitSafetyPort,
    IssueResolver,
    LogChannel,
    OpenCodePreflightPort,
    ProcessRunner,
    RunStorePort,
    Sleeper,
    TargetLease,
    TargetLeaseFactory,
)

NOW = datetime(2026, 9, 13, 8, 9, 10, 123456, tzinfo=UTC)
LATER = datetime(2026, 9, 13, 8, 9, 11, 654321, tzinfo=UTC)
WORKSPACE_ROOT = Path("/workspaces/opencode-tools")
TARGET_ROOT = WORKSPACE_ROOT / "backend"
RUNTIME_ROOT = WORKSPACE_ROOT / ".opencode-tools"


def _workspace() -> Workspace:
    return Workspace(root=WORKSPACE_ROOT)


def _target() -> TargetRepository:
    return TargetRepository(
        root=TARGET_ROOT,
        workspace_relative=Path("backend"),
        git_common_dir=TARGET_ROOT / ".git",
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
    return IssueLocator(repository_identity=_identity(), number=6)


def _process_spec() -> ProcessSpec:
    return ProcessSpec(
        argv=("/usr/bin/git", "status"),
        cwd=TARGET_ROOT,
        stdin=None,
        timeout_seconds=30,
        termination_grace_seconds=5,
    )


def _process_result() -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/git", "status"),
        cwd=TARGET_ROOT,
        started_at=NOW,
        finished_at=LATER,
        duration_ns=1_000_000,
        return_code=0,
        timed_out=False,
        termination_confirmed=True,
        log_path=Path("architect-provider-attempt-1.log"),
        stdout_byte_count=0,
        stdout_sha256="stdout-digest",
        stderr_byte_count=0,
        stderr_sha256="stderr-digest",
        outcome=RunOutcome.SUCCEEDED,
    )


def _agent_result() -> AgentResult:
    return AgentResult(
        role=AgentRole.ARCHITECT,
        phase=PipelinePhase.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        process=_process_result(),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.ARCHITECT,
            body="Ready to implement the issue.",
        ),
        session_id="session-001",
        verified_agent="architect",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )


def _git_state() -> GitState:
    return GitState(
        root=TARGET_ROOT,
        branch="feat/ports",
        head="0123456789abcdef",
        porcelain_summary="clean",
        staged=(),
        unstaged=(),
        untracked=(),
        fingerprint="fingerprint-001",
    )


def _git_check() -> GitCheckRecord:
    return GitCheckRecord(
        sequence=0,
        purpose="baseline",
        process_results=(_process_result(),),
        state=_git_state(),
        safety_status=GitSafetyStatus.SAFE,
    )


def _run_record() -> RunRecord:
    return RunRecord(
        schema_version=1,
        run_id="run-001",
        artifact_path=RUNTIME_ROOT / "runs/run-001/run.json",
        workspace=_workspace(),
        target=_target(),
        issue_number=6,
        config={"source": "defaults"},
        environment={"tool_version": "0.1.0"},
        started_at=NOW,
        current_phase=PipelinePhase.PREFLIGHT,
        persistence_status=PersistenceStatus.OK,
    )


class RecordingAttemptLogSink:
    """A minimal `AttemptLogSink` fake that records writes in call order."""

    def __init__(self, path: Path = Path("attempt.log")) -> None:
        self._path = path
        self.writes: list[tuple[LogChannel, bytes, datetime]] = []
        self.closed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        channel: LogChannel,
        payload: bytes,
        timestamp: datetime,
    ) -> None:
        self.writes.append((channel, payload, timestamp))

    def close(self) -> None:
        self.closed = True


class FakeClock:
    """A `Clock` fake with a fixed wall-clock time and a step counter."""

    def __init__(self, *, now: datetime, start_ns: int = 0) -> None:
        self._now = now
        self._monotonic_ns = start_ns

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        self._monotonic_ns += 1
        return self._monotonic_ns


class AdvancingSleeper:
    """A `Sleeper` fake that records requested delays without blocking."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class ScriptedProcessRunner:
    """A `ProcessRunner` fake returning one canned result per call."""

    def __init__(self, result: ProcessResult) -> None:
        self._result = result
        self.calls: list[tuple[ProcessSpec, AttemptLogSink]] = []

    def run(self, spec: ProcessSpec, *, sink: AttemptLogSink) -> ProcessResult:
        self.calls.append((spec, sink))
        return self._result


class ScriptedAgentRunner:
    """An `AgentRunner` fake returning one canned result per call."""

    def __init__(self, result: AgentResult) -> None:
        self._result = result
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
        return self._result


class RecordingGitSafetyPort:
    """A `GitSafetyPort` fake returning canned target and checkpoint data."""

    def __init__(self, target: TargetRepository, check: GitCheckRecord) -> None:
        self._target = target
        self._check = check
        self.resolve_calls: list[tuple[Workspace, Path]] = []
        self.runtime_location_calls: list[Path] = []
        self.check_calls: list[
            tuple[TargetRepository, int, str, AgentRole | None, GitState | None]
        ] = []

    def check_runtime_location(self, runtime_root: Path) -> None:
        self.runtime_location_calls.append(runtime_root)

    def resolve_target(
        self,
        workspace: Workspace,
        target_root: Path,
    ) -> TargetRepository:
        self.resolve_calls.append((workspace, target_root))
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
        self.check_calls.append((target, sequence, purpose, role, baseline))
        return self._check


class RecordingIssueResolver:
    """An `IssueResolver` fake returning canned identity and locator data."""

    def __init__(self, identity: RepositoryIdentity, locator: IssueLocator) -> None:
        self._identity = identity
        self._locator = locator
        self.resolve_calls: list[TargetRepository] = []
        self.locate_calls: list[tuple[RepositoryIdentity, int]] = []

    def resolve_repository(self, target: TargetRepository) -> RepositoryIdentity:
        self.resolve_calls.append(target)
        return self._identity

    def locate_issue(
        self,
        repository_identity: RepositoryIdentity,
        issue_number: int,
    ) -> IssueLocator:
        self.locate_calls.append((repository_identity, issue_number))
        return self._locator


class RecordingOpenCodePreflightPort:
    """An `OpenCodePreflightPort` fake recording `verify()`/`recheck()`
    calls, each independently scriptable to raise -- only the canonical
    control-plane digest `verify` returns ever crosses this boundary,
    matching the Protocol's own "no other evidence crosses this boundary"
    contract."""

    def __init__(
        self,
        *,
        digest: str = "digest-001",
        error: Exception | None = None,
        recheck_error: Exception | None = None,
    ) -> None:
        self._digest = digest
        self._error = error
        self._recheck_error = recheck_error
        self.calls = 0
        self.recheck_calls: list[str] = []

    def verify(self) -> str:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._digest

    def recheck(self, expected_digest: str) -> None:
        self.recheck_calls.append(expected_digest)
        if self._recheck_error is not None:
            raise self._recheck_error


class RecordingRunStorePort:
    """A `RunStorePort` fake recording every call without touching disk."""

    def __init__(self, artifact_root: Path, persistence: PersistenceStatus) -> None:
        self._artifact_root = artifact_root
        self._persistence = persistence
        self.initialize_calls: list[tuple[Workspace, str]] = []
        self.sink_calls: list[tuple[AgentRole, int | None, int]] = []
        self.persisted: list[RunRecord] = []
        self.staged: list[RunRecord] = []
        self.committed = 0
        self.aborted = 0

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        self.initialize_calls.append((workspace, run_id))
        return self._artifact_root

    def open_attempt_sink(
        self,
        role: AgentRole,
        review_cycle: int | None,
        provider_attempt: int,
    ) -> AttemptLogSink:
        self.sink_calls.append((role, review_cycle, provider_attempt))
        return RecordingAttemptLogSink()

    def persist(self, record: RunRecord) -> PersistenceStatus:
        self.persisted.append(record)
        return self._persistence

    def stage_final(self, record: RunRecord) -> PersistenceStatus:
        self.staged.append(record)
        return self._persistence

    def commit_final(self) -> PersistenceStatus:
        self.committed += 1
        return self._persistence

    def abort_final(self) -> None:
        self.aborted += 1


class FakeTargetLease:
    """A `TargetLease` fake recording whether it was entered and exited."""

    def __init__(self) -> None:
        self.entered = False
        self.exited_with: BaseException | None = None
        self.exit_called = False
        self.quarantine_calls: list[str] = []

    def __enter__(self) -> Self:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exit_called = True
        self.exited_with = exc

    def quarantine(self, reason: str) -> None:
        self.quarantine_calls.append(reason)


class FakeTargetLeaseFactory:
    """A `TargetLeaseFactory` fake returning one canned lease per call."""

    def __init__(self, lease: FakeTargetLease) -> None:
        self._lease = lease
        self.calls: list[tuple[Path, Path, str]] = []

    def acquire(
        self,
        target_root: Path,
        runtime_root: Path,
        run_id: str,
    ) -> TargetLease:
        self.calls.append((target_root, runtime_root, run_id))
        return self._lease


def test_clock_port_reports_utc_time_and_monotonic_steps() -> None:
    fake = FakeClock(now=NOW)
    clock: Clock = fake

    assert clock.now() == NOW
    assert clock.monotonic_ns() == 1
    assert clock.monotonic_ns() == 2


def test_sleeper_port_records_requested_delays_without_blocking() -> None:
    fake = AdvancingSleeper()
    sleeper: Sleeper = fake

    sleeper.sleep(0.5)
    sleeper.sleep(1.5)

    assert fake.slept == [0.5, 1.5]


def test_attempt_log_sink_port_records_writes_and_close() -> None:
    fake = RecordingAttemptLogSink()
    sink: AttemptLogSink = fake

    sink.write("stdout", b"line one\n", NOW)
    sink.write("stderr", b"warning\n", LATER)
    sink.close()

    assert fake.writes == [
        ("stdout", b"line one\n", NOW),
        ("stderr", b"warning\n", LATER),
    ]
    assert fake.closed is True


def test_process_runner_port_receives_the_spec_and_sink_it_was_given() -> None:
    spec = _process_spec()
    sink = RecordingAttemptLogSink()
    canned_result = _process_result()
    fake = ScriptedProcessRunner(canned_result)
    runner: ProcessRunner = fake

    result = runner.run(spec, sink=sink)

    assert result is canned_result
    assert fake.calls == [(spec, sink)]


def test_agent_runner_port_receives_role_prompt_and_attempt_counters() -> None:
    workspace = _workspace()
    sink = RecordingAttemptLogSink()
    canned_result = _agent_result()
    fake = ScriptedAgentRunner(canned_result)
    runner: AgentRunner = fake

    result = runner.run(
        AgentRole.ARCHITECT,
        "Implement the issue.",
        workspace,
        review_cycle=None,
        provider_attempt=1,
        sink=sink,
    )

    assert result is canned_result
    assert fake.calls == [
        (AgentRole.ARCHITECT, "Implement the issue.", workspace, None, 1),
    ]


def test_git_safety_port_resolves_targets_and_reports_checkpoints() -> None:
    workspace = _workspace()
    target = _target()
    check = _git_check()
    fake = RecordingGitSafetyPort(target, check)
    git_safety: GitSafetyPort = fake

    git_safety.check_runtime_location(RUNTIME_ROOT)
    resolved = git_safety.resolve_target(workspace, TARGET_ROOT)
    reported = git_safety.check(target, sequence=0, purpose="baseline")

    assert resolved is target
    assert reported is check
    assert fake.runtime_location_calls == [RUNTIME_ROOT]
    assert fake.resolve_calls == [(workspace, TARGET_ROOT)]
    assert fake.check_calls == [(target, 0, "baseline", None, None)]


def test_issue_resolver_port_resolves_repository_then_locates_the_issue() -> None:
    target = _target()
    identity = _identity()
    locator = _locator()
    fake = RecordingIssueResolver(identity, locator)
    resolver: IssueResolver = fake

    resolved_identity = resolver.resolve_repository(target)
    resolved_locator = resolver.locate_issue(resolved_identity, 6)

    assert resolved_identity is identity
    assert resolved_locator is locator
    assert fake.resolve_calls == [target]
    assert fake.locate_calls == [(identity, 6)]


def test_opencode_preflight_port_verifies_once_and_returns_the_digest() -> None:
    fake = RecordingOpenCodePreflightPort(digest="digest-001")
    preflight: OpenCodePreflightPort = fake

    digest = preflight.verify()

    assert digest == "digest-001"
    assert fake.calls == 1


def test_opencode_preflight_port_propagates_a_failed_verification() -> None:
    error = PreflightError("opencode.version_mismatch", "unexpected version")
    fake = RecordingOpenCodePreflightPort(error=error)
    preflight: OpenCodePreflightPort = fake

    with pytest.raises(PreflightError):
        preflight.verify()

    assert fake.calls == 1


def test_opencode_preflight_port_rechecks_the_same_digest() -> None:
    fake = RecordingOpenCodePreflightPort(digest="digest-001")
    preflight: OpenCodePreflightPort = fake

    preflight.recheck("digest-001")
    preflight.recheck("digest-001")

    assert fake.recheck_calls == ["digest-001", "digest-001"]


def test_opencode_preflight_port_propagates_a_control_plane_drift() -> None:
    drift = ProtocolError("opencode.control_plane_drift", "the digest changed")
    fake = RecordingOpenCodePreflightPort(recheck_error=drift)
    preflight: OpenCodePreflightPort = fake

    with pytest.raises(ProtocolError):
        preflight.recheck("digest-001")

    assert fake.recheck_calls == ["digest-001"]


def test_run_store_port_initializes_opens_sinks_and_persists_records() -> None:
    workspace = _workspace()
    artifact_root = RUNTIME_ROOT / "runs/run-001"
    record = _run_record()
    fake = RecordingRunStorePort(artifact_root, PersistenceStatus.OK)
    store: RunStorePort = fake

    path = store.initialize(workspace, "run-001")
    sink = store.open_attempt_sink(AgentRole.ARCHITECT, None, 1)
    status = store.persist(record)
    staged_status = store.stage_final(record)
    committed_status = store.commit_final()
    store.abort_final()

    assert path == artifact_root
    assert isinstance(sink, RecordingAttemptLogSink)
    assert status is PersistenceStatus.OK
    assert staged_status is PersistenceStatus.OK
    assert committed_status is PersistenceStatus.OK
    assert fake.initialize_calls == [(workspace, "run-001")]
    assert fake.sink_calls == [(AgentRole.ARCHITECT, None, 1)]
    assert fake.persisted == [record]
    assert fake.staged == [record]
    assert fake.committed == 1
    assert fake.aborted == 1


def test_target_lease_factory_port_acquires_a_context_managed_lease() -> None:
    lease = FakeTargetLease()
    fake = FakeTargetLeaseFactory(lease)
    factory: TargetLeaseFactory = fake

    with factory.acquire(TARGET_ROOT, RUNTIME_ROOT, "run-001") as acquired:
        assert lease.entered is True
        assert lease.exit_called is False
        acquired.quarantine("unconfirmed termination")

    assert fake.calls == [(TARGET_ROOT, RUNTIME_ROOT, "run-001")]
    assert lease.exit_called is True
    assert lease.exited_with is None
    assert lease.quarantine_calls == ["unconfirmed termination"]

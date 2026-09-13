"""Substitutable `Protocol` boundaries for every I/O-touching collaborator.

Each `Protocol` here freezes a type signature only: no method body performs
I/O, retries, sleeps, or owns lifecycle, and none imports a concrete adapter.
Fake or recording implementations built directly against these contracts must
be enough for deterministic tests of every applicative layer that depends on
them.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Protocol, Self

from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    GitCheckRecord,
    GitState,
    IssueLocator,
    PersistenceStatus,
    ProcessResult,
    ProcessSpec,
    RepositoryIdentity,
    RunRecord,
    TargetRepository,
    Workspace,
)

type LogChannel = Literal["stdout", "stderr", "runner"]


class Clock(Protocol):
    """A source of wall-clock and monotonic time, replaceable by a fake."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC `datetime`."""
        ...

    def monotonic_ns(self) -> int:
        """Return a monotonic nanosecond count, used only for durations."""
        ...


class Sleeper(Protocol):
    """A delay primitive, replaceable to keep retry tests instantaneous."""

    def sleep(self, seconds: float) -> None:
        """Block the caller for `seconds`, or record the request in a fake."""
        ...


class AttemptLogSink(Protocol):
    """An already-open, append-only destination for one attempt's streams.

    The caller opens the sink, via `RunStorePort.open_attempt_sink`, before
    any child process is spawned; a failure to open is the caller's concern,
    not this contract's.
    """

    @property
    def path(self) -> Path:
        """Return the sink's own workspace-relative path.

        `ProcessRunner` echoes this into `ProcessResult.log_path` without
        knowing the naming convention (role, review cycle, attempt) that
        produced it.
        """
        ...

    def write(
        self,
        channel: LogChannel,
        payload: bytes,
        timestamp: datetime,
    ) -> None:
        """Append one framed record for `channel` at `timestamp`."""
        ...

    def close(self) -> None:
        """Flush and finalize the sink; idempotent for a closed sink."""
        ...


class ProcessRunner(Protocol):
    """A generic bounded child-process boundary, unaware of agents or Git."""

    def run(self, spec: ProcessSpec, *, sink: AttemptLogSink) -> ProcessResult:
        """Run the already-validated `spec` to completion or timeout."""
        ...


class AgentRunner(Protocol):
    """Invokes one primary agent role and returns its technical result."""

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
        """Run `role` once with `prompt` on stdin and classify the outcome."""
        ...


class GitSafetyPort(Protocol):
    """Target resolution and content-sensitive Git safety checkpoints."""

    def resolve_target(
        self,
        workspace: Workspace,
        target_root: Path,
    ) -> TargetRepository:
        """Validate `target_root` and return its resolved identity."""
        ...

    def check(
        self,
        target: TargetRepository,
        *,
        sequence: int,
        purpose: str,
        baseline: GitState | None = None,
    ) -> GitCheckRecord:
        """Capture a checkpoint for `purpose`, comparing against `baseline`."""
        ...


class IssueResolver(Protocol):
    """Resolves the GitHub repository and issue addressed by this run."""

    def resolve_repository(self, target: TargetRepository) -> RepositoryIdentity:
        """Resolve host/owner/repository from override or Git remotes."""
        ...

    def locate_issue(
        self,
        repository_identity: RepositoryIdentity,
        issue_number: int,
    ) -> IssueLocator:
        """Pair a resolved repository identity with the requested issue."""
        ...


class RunStorePort(Protocol):
    """Runtime artifact layout, attempt sinks, and atomic `run.json` writes."""

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        """Create the run's artifact directory and return its path."""
        ...

    def open_attempt_sink(
        self,
        role: AgentRole,
        review_cycle: int | None,
        provider_attempt: int,
    ) -> AttemptLogSink:
        """Open a fresh, already-writable sink for one attempt's streams."""
        ...

    def persist(self, record: RunRecord) -> PersistenceStatus:
        """Atomically replace `run.json` with `record` and report the result."""
        ...


class TargetLease(Protocol):
    """A held, non-blocking lock on one canonical target, released on exit."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class TargetLeaseFactory(Protocol):
    """Acquires the non-blocking lease for one canonical target."""

    def acquire(
        self,
        target_root: Path,
        runtime_root: Path,
        run_id: str,
    ) -> TargetLease:
        """Acquire the lease for `target_root`, failing closed if held."""
        ...


__all__ = (
    "AgentRunner",
    "AttemptLogSink",
    "Clock",
    "GitSafetyPort",
    "IssueResolver",
    "LogChannel",
    "ProcessRunner",
    "RunStorePort",
    "Sleeper",
    "TargetLease",
    "TargetLeaseFactory",
)

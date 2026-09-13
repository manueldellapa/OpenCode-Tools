"""Typed, immutable domain contracts shared by policies and adapters.

This module deliberately contains no filesystem, process, network, parsing,
retry, transition, or persistence behavior.  Constructors enforce only local,
side-effect-free invariants; adapters remain responsible for proving facts
about the outside world before creating these values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import cast


class AgentRole(StrEnum):
    """The only primary agent roles in the v0.1 pipeline."""

    ARCHITECT = "ARCHITECT"
    CODER = "CODER"
    REVIEWER = "REVIEWER"


class PipelinePhase(StrEnum):
    """A position in the Python-owned lifecycle, never an outcome."""

    PREFLIGHT = "PREFLIGHT"
    ARCHITECT = "ARCHITECT"
    CODER = "CODER"
    REVIEWER = "REVIEWER"
    POSTFLIGHT = "POSTFLIGHT"
    FINALIZATION = "FINALIZATION"
    FINISHED = "FINISHED"


class AgentStatus(StrEnum):
    """A terminal status reported by an architect, coder, or failed reviewer."""

    READY = "READY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReviewStatus(StrEnum):
    """A reviewer decision; changes requested is not an error outcome."""

    APPROVED = "APPROVED"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"


class RunOutcome(StrEnum):
    """The canonical, mutually distinguishable technical run outcomes."""

    SUCCEEDED = "SUCCEEDED"
    CONFIG_ERROR = "CONFIG_ERROR"
    PREFLIGHT_ERROR = "PREFLIGHT_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    PROCESS_ERROR = "PROCESS_ERROR"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    AGENT_REPORTED_FAILURE = "AGENT_REPORTED_FAILURE"
    REVIEW_CYCLES_EXHAUSTED = "REVIEW_CYCLES_EXHAUSTED"
    GIT_SAFETY_ERROR = "GIT_SAFETY_ERROR"
    LOGGING_ERROR = "LOGGING_ERROR"
    INTERRUPTED = "INTERRUPTED"


class FinalStatus(StrEnum):
    """The single final status emitted by the Python finalizer."""

    APPROVED = "APPROVED"
    FAILED = "FAILED"


class GitSafetyStatus(StrEnum):
    """Git safety evidence, orthogonal to a process outcome."""

    SAFE = "SAFE"
    UNSAFE = "UNSAFE"
    INDETERMINATE = "INDETERMINATE"


class PersistenceStatus(StrEnum):
    """Artifact persistence state, separate from the triggering outcome."""

    OK = "OK"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"


_PROCESS_RESULT_OUTCOMES = frozenset(
    {
        RunOutcome.SUCCEEDED,
        RunOutcome.TIMEOUT,
        RunOutcome.PROCESS_ERROR,
        RunOutcome.LOGGING_ERROR,
        RunOutcome.INTERRUPTED,
    }
)
_PHASE_BY_ROLE = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}


type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type FrozenJsonValue = (
    JsonScalar | tuple[FrozenJsonValue, ...] | Mapping[str, FrozenJsonValue]
)


def _require_non_empty(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_optional_non_empty(value: object, field_name: str) -> None:
    if value is not None:
        _require_non_empty(value, field_name)


def _require_exact_enum(
    value: object,
    enum_type: type[StrEnum],
    field_name: str,
) -> None:
    if type(value) is not enum_type:
        raise TypeError(f"{field_name} must be {enum_type.__name__}")


def _require_optional_enum(
    value: object,
    enum_type: type[StrEnum],
    field_name: str,
) -> None:
    if value is not None:
        _require_exact_enum(value, enum_type, field_name)


def _require_int(value: object, field_name: str, *, minimum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")


def _require_optional_int(
    value: object,
    field_name: str,
    *,
    minimum: int,
) -> None:
    if value is not None:
        _require_int(value, field_name, minimum=minimum)


def _require_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a boolean")


def _require_optional_bool(value: object, field_name: str) -> None:
    if value is not None:
        _require_bool(value, field_name)


def _finite_number(value: object, field_name: str, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number")
    normalized = float(value)
    if not isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    if positive and normalized <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return normalized


def _require_utc(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


def _require_optional_utc(value: object, field_name: str) -> None:
    if value is not None:
        _require_utc(value, field_name)


def _require_path(value: object, field_name: str, *, absolute: bool) -> None:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a Path")
    if absolute and not value.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    if not absolute and value.is_absolute():
        raise ValueError(f"{field_name} must be relative")
    if ".." in value.parts:
        raise ValueError(f"{field_name} must not contain '..'")


def _copy_strings(
    values: object,
    field_name: str,
    *,
    allow_empty_items: bool,
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{field_name} must be a collection of strings")
    copied: tuple[object, ...] = tuple(values)
    for value in copied:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} must contain only strings")
        if not allow_empty_items and not value:
            raise ValueError(f"{field_name} must not contain empty strings")
    return cast(tuple[str, ...], copied)


def _copy_records[T](
    values: object,
    record_type: type[T],
    field_name: str,
) -> tuple[T, ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{field_name} must be a collection")
    copied: tuple[object, ...] = tuple(values)
    if not all(isinstance(value, record_type) for value in copied):
        raise TypeError(f"{field_name} contains an invalid record")
    return cast(tuple[T, ...], copied)


def _freeze_json(value: object, field_name: str) -> FrozenJsonValue:
    if value is None or type(value) in (bool, int, str):
        return cast(JsonScalar, value)
    if type(value) is float:
        if not isfinite(value):
            raise ValueError(f"{field_name} contains a non-finite float")
        return value
    if isinstance(value, (tuple, list)):
        return tuple(
            _freeze_json(item, f"{field_name}[]")
            for item in cast(tuple[object, ...] | list[object], value)
        )
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJsonValue] = {}
        for key, item in cast(Mapping[object, object], value).items():
            if type(key) is not str:
                raise TypeError(f"{field_name} must have string keys")
            frozen[key] = _freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    raise TypeError(f"{field_name} contains a non-JSON value")


def _freeze_mapping(
    value: object,
    field_name: str,
) -> Mapping[str, FrozenJsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    frozen = _freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return frozen


def _copy_json_mappings(
    values: object,
    field_name: str,
) -> tuple[Mapping[str, FrozenJsonValue], ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{field_name} must be a collection of mappings")
    copied: tuple[object, ...] = tuple(values)
    return tuple(_freeze_mapping(value, f"{field_name}[]") for value in copied)


def _copy_string_mapping(value: object, field_name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    copied: dict[str, str] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if type(key) is not str or not isinstance(item, str):
            raise TypeError(f"{field_name} must contain string pairs")
        copied[key] = item
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class Workspace:
    """A pre-canonicalized workspace root."""

    root: Path

    def __post_init__(self) -> None:
        _require_path(self.root, "root", absolute=True)


@dataclass(frozen=True, slots=True)
class TargetRepository:
    """A target Git repository kept distinct from its workspace."""

    root: Path
    workspace_relative: Path
    git_common_dir: Path

    def __post_init__(self) -> None:
        _require_path(self.root, "root", absolute=True)
        _require_path(
            self.workspace_relative,
            "workspace_relative",
            absolute=False,
        )
        _require_path(self.git_common_dir, "git_common_dir", absolute=True)


@dataclass(frozen=True, slots=True)
class RunRequest:
    """The CLI-facing request proven valid before any agent or artifact I/O.

    `target_root` is only the canonicalized target path; the Git top-level
    proof that yields a full `TargetRepository` (with `git_common_dir`)
    happens later, in `GitSafetyPort.resolve_target`.
    """

    issue_number: int
    workspace: Workspace
    target_root: Path

    def __post_init__(self) -> None:
        _require_int(self.issue_number, "issue_number", minimum=1)
        if not isinstance(self.workspace, Workspace):
            raise TypeError("workspace must be Workspace")
        _require_path(self.target_root, "target_root", absolute=True)
        if not self.target_root.is_relative_to(self.workspace.root):
            raise ValueError("target_root must be contained in workspace")


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """A normalized GitHub repository identity and its resolution source."""

    host: str
    owner: str
    repository: str
    source: str
    remote_name: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("host", "owner", "repository", "source"):
            _require_non_empty(getattr(self, field_name), field_name)
        _require_optional_non_empty(self.remote_name, "remote_name")
        if self.host != self.host.lower():
            raise ValueError("host must be lowercase")


@dataclass(frozen=True, slots=True)
class IssueLocator:
    """The repository and positive issue number known before the architect."""

    repository_identity: RepositoryIdentity
    number: int

    def __post_init__(self) -> None:
        if not isinstance(self.repository_identity, RepositoryIdentity):
            raise TypeError("repository_identity must be RepositoryIdentity")
        _require_int(self.number, "number", minimum=1)


@dataclass(frozen=True, slots=True)
class IssueRef:
    """The issue identity returned by a validated protocol-v1 envelope."""

    locator: IssueLocator
    url: str
    title: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.locator, IssueLocator):
            raise TypeError("locator must be IssueLocator")
        _require_non_empty(self.url, "url")
        _require_non_empty(self.title, "title")
        _require_int(self.schema_version, "schema_version", minimum=1)
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")

        identity = self.locator.repository_identity
        expected_path = (
            f"/{identity.owner}/{identity.repository}/issues/{self.locator.number}"
        )
        expected_url = f"https://{identity.host}{expected_path}"
        if self.url != expected_url:
            raise ValueError("url must be the canonical HTTPS URL for locator")


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """Pure inputs for a bounded child process invocation.

    This value intentionally is not JSON-convertible: stdin and environment
    overrides are runtime inputs and must not be persisted accidentally.
    """

    argv: tuple[str, ...]
    cwd: Path
    stdin: str | bytes | None
    timeout_seconds: float
    termination_grace_seconds: float
    environment_overrides: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        argv = _copy_strings(
            self.argv,
            "argv",
            allow_empty_items=True,
        )
        if not argv:
            raise ValueError("argv must not be empty")
        executable = Path(argv[0])
        if not executable.is_absolute():
            raise ValueError("argv[0] must be an absolute executable path")
        if self.stdin is not None and not isinstance(self.stdin, (str, bytes)):
            raise TypeError("stdin must be str, bytes, or None")
        _require_path(self.cwd, "cwd", absolute=True)
        timeout = _finite_number(
            self.timeout_seconds,
            "timeout_seconds",
            positive=True,
        )
        grace = _finite_number(
            self.termination_grace_seconds,
            "termination_grace_seconds",
            positive=True,
        )
        environment = _copy_string_mapping(
            self.environment_overrides,
            "environment_overrides",
        )

        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "termination_grace_seconds", grace)
        object.__setattr__(self, "environment_overrides", environment)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Technical process evidence without agent-level semantics."""

    command: tuple[str, ...]
    cwd: Path
    started_at: datetime
    finished_at: datetime
    duration_ns: int
    return_code: int | None
    timed_out: bool
    termination_confirmed: bool | None
    log_path: Path
    stdout_byte_count: int
    stdout_sha256: str
    stderr_byte_count: int
    stderr_sha256: str
    outcome: RunOutcome

    def __post_init__(self) -> None:
        command = _copy_strings(
            self.command,
            "command",
            allow_empty_items=True,
        )
        if not command:
            raise ValueError("command must not be empty")
        _require_path(self.cwd, "cwd", absolute=True)
        _require_utc(self.started_at, "started_at")
        _require_utc(self.finished_at, "finished_at")
        _require_int(self.duration_ns, "duration_ns", minimum=0)
        if self.return_code is not None and type(self.return_code) is not int:
            raise TypeError("return_code must be an integer or None")
        _require_bool(self.timed_out, "timed_out")
        _require_optional_bool(
            self.termination_confirmed,
            "termination_confirmed",
        )
        _require_path(self.log_path, "log_path", absolute=False)
        _require_int(self.stdout_byte_count, "stdout_byte_count", minimum=0)
        _require_non_empty(self.stdout_sha256, "stdout_sha256")
        _require_int(self.stderr_byte_count, "stderr_byte_count", minimum=0)
        _require_non_empty(self.stderr_sha256, "stderr_sha256")
        _require_exact_enum(self.outcome, RunOutcome, "outcome")
        if self.outcome not in _PROCESS_RESULT_OUTCOMES:
            raise ValueError("outcome is not a process-level outcome")
        if self.timed_out != (self.outcome is RunOutcome.TIMEOUT):
            raise ValueError("timed_out must match a TIMEOUT outcome")
        if self.outcome is RunOutcome.SUCCEEDED and (
            self.return_code != 0 or self.termination_confirmed is not True
        ):
            raise ValueError(
                "SUCCEEDED requires return_code 0 and confirmed termination"
            )
        object.__setattr__(self, "command", command)


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    """A trusted provider-classifier fact produced by a versioned adapter."""

    source: str
    signature: str
    retryable: bool
    status_code: int | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.source, "source")
        _require_non_empty(self.signature, "signature")
        _require_bool(self.retryable, "retryable")
        _require_optional_int(self.status_code, "status_code", minimum=0)
        _require_optional_non_empty(self.code, "code")


@dataclass(frozen=True, slots=True)
class ParsedAgentResponse:
    """A transport-independent, terminal protocol response."""

    role: AgentRole
    body: str
    agent_status: AgentStatus | None = None
    review_status: ReviewStatus | None = None
    issue_ref: IssueRef | None = None
    protocol_version: int = 1

    def __post_init__(self) -> None:
        _require_exact_enum(self.role, AgentRole, "role")
        if not isinstance(self.body, str):
            raise TypeError("body must be a string")
        _require_optional_enum(self.agent_status, AgentStatus, "agent_status")
        _require_optional_enum(self.review_status, ReviewStatus, "review_status")
        if self.issue_ref is not None and not isinstance(self.issue_ref, IssueRef):
            raise TypeError("issue_ref must be IssueRef or None")
        _require_int(self.protocol_version, "protocol_version", minimum=1)
        if self.protocol_version != 1:
            raise ValueError("protocol_version must be 1")


@dataclass(frozen=True, slots=True)
class AgentResult:
    """One agent attempt result with technical and protocol facts separated."""

    role: AgentRole
    phase: PipelinePhase
    review_cycle: int | None
    provider_attempt: int
    process: ProcessResult
    terminal_response: ParsedAgentResponse | None
    session_id: str | None
    verified_agent: str | None
    provider_diagnostic: ProviderDiagnostic | None
    outcome: RunOutcome

    def __post_init__(self) -> None:
        _require_exact_enum(self.role, AgentRole, "role")
        _require_exact_enum(self.phase, PipelinePhase, "phase")
        _require_optional_int(self.review_cycle, "review_cycle", minimum=1)
        _require_int(self.provider_attempt, "provider_attempt", minimum=1)
        if self.phase is not _PHASE_BY_ROLE[self.role]:
            raise ValueError("phase must match role")
        if self.role is AgentRole.ARCHITECT:
            if self.review_cycle is not None:
                raise ValueError("architect review_cycle must be None")
        elif self.review_cycle is None:
            raise ValueError("coder and reviewer require a review_cycle")
        if not isinstance(self.process, ProcessResult):
            raise TypeError("process must be ProcessResult")
        if self.terminal_response is not None:
            if not isinstance(self.terminal_response, ParsedAgentResponse):
                raise TypeError("terminal_response must be ParsedAgentResponse or None")
            if self.terminal_response.role is not self.role:
                raise ValueError("terminal_response role must match role")
        _require_optional_non_empty(self.session_id, "session_id")
        _require_optional_non_empty(self.verified_agent, "verified_agent")
        if self.provider_diagnostic is not None and not isinstance(
            self.provider_diagnostic,
            ProviderDiagnostic,
        ):
            raise TypeError("provider_diagnostic must be ProviderDiagnostic or None")
        _require_exact_enum(self.outcome, RunOutcome, "outcome")


@dataclass(frozen=True, slots=True)
class GitState:
    """A content-sensitive Git snapshot or a partial failed-probe snapshot."""

    root: Path
    branch: str | None
    head: str | None
    porcelain_summary: str
    staged: tuple[str, ...]
    unstaged: tuple[str, ...]
    untracked: tuple[str, ...]
    fingerprint: str | None
    fingerprint_version: str = "git-state-v1"

    def __post_init__(self) -> None:
        _require_path(self.root, "root", absolute=True)
        _require_optional_non_empty(self.branch, "branch")
        _require_optional_non_empty(self.head, "head")
        if not isinstance(self.porcelain_summary, str):
            raise TypeError("porcelain_summary must be a string")
        object.__setattr__(
            self,
            "staged",
            _copy_strings(self.staged, "staged", allow_empty_items=False),
        )
        object.__setattr__(
            self,
            "unstaged",
            _copy_strings(self.unstaged, "unstaged", allow_empty_items=False),
        )
        object.__setattr__(
            self,
            "untracked",
            _copy_strings(self.untracked, "untracked", allow_empty_items=False),
        )
        _require_optional_non_empty(self.fingerprint, "fingerprint")
        _require_non_empty(self.fingerprint_version, "fingerprint_version")
        if self.fingerprint_version != "git-state-v1":
            raise ValueError("fingerprint_version must be git-state-v1")


@dataclass(frozen=True, slots=True)
class GitCheckRecord:
    """A Git safety check and the process evidence used to derive it."""

    sequence: int
    purpose: str
    process_results: tuple[ProcessResult, ...]
    state: GitState
    safety_status: GitSafetyStatus
    compared_to: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.sequence, "sequence", minimum=0)
        _require_non_empty(self.purpose, "purpose")
        object.__setattr__(
            self,
            "process_results",
            _copy_records(
                self.process_results,
                ProcessResult,
                "process_results",
            ),
        )
        if not isinstance(self.state, GitState):
            raise TypeError("state must be GitState")
        _require_exact_enum(
            self.safety_status,
            GitSafetyStatus,
            "safety_status",
        )
        if self.safety_status is GitSafetyStatus.SAFE and (
            self.state.branch is None
            or self.state.head is None
            or self.state.fingerprint is None
        ):
            raise ValueError("SAFE requires complete branch, HEAD, and fingerprint")
        _require_optional_non_empty(self.compared_to, "compared_to")


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One provider attempt within a logical role invocation."""

    logical_invocation_id: str
    role: AgentRole
    review_cycle: int | None
    provider_attempt: int
    git_before: GitCheckRecord
    git_after: GitCheckRecord
    agent_result: AgentResult
    retry_decision: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.logical_invocation_id, "logical_invocation_id")
        _require_exact_enum(self.role, AgentRole, "role")
        _require_optional_int(self.review_cycle, "review_cycle", minimum=1)
        _require_int(self.provider_attempt, "provider_attempt", minimum=1)
        if not isinstance(self.git_before, GitCheckRecord):
            raise TypeError("git_before must be GitCheckRecord")
        if not isinstance(self.git_after, GitCheckRecord):
            raise TypeError("git_after must be GitCheckRecord")
        if not isinstance(self.agent_result, AgentResult):
            raise TypeError("agent_result must be AgentResult")
        if self.agent_result.role is not self.role:
            raise ValueError("agent_result role must match role")
        if self.agent_result.review_cycle != self.review_cycle:
            raise ValueError("agent_result review_cycle must match review_cycle")
        if self.agent_result.provider_attempt != self.provider_attempt:
            raise ValueError(
                "agent_result provider_attempt must match provider_attempt"
            )
        _require_bool(self.retry_decision, "retry_decision")


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    """One ordered, serializable error cause without raw sensitive output."""

    sequence: int
    timestamp: datetime
    phase: PipelinePhase
    outcome: RunOutcome
    code: str
    message: str
    related_record: str | None = None
    technical_detail: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.sequence, "sequence", minimum=0)
        _require_utc(self.timestamp, "timestamp")
        _require_exact_enum(self.phase, PipelinePhase, "phase")
        _require_exact_enum(self.outcome, RunOutcome, "outcome")
        if self.outcome is RunOutcome.SUCCEEDED:
            raise ValueError("ErrorRecord outcome must describe an error")
        _require_non_empty(self.code, "code")
        _require_non_empty(self.message, "message")
        _require_optional_non_empty(self.related_record, "related_record")
        _require_optional_non_empty(self.technical_detail, "technical_detail")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """An immutable in-memory snapshot from which run.json can later be built."""

    schema_version: int
    run_id: str
    artifact_path: Path
    workspace: Workspace
    target: TargetRepository
    issue_number: int
    config: Mapping[str, FrozenJsonValue]
    environment: Mapping[str, FrozenJsonValue]
    started_at: datetime
    current_phase: PipelinePhase
    persistence_status: PersistenceStatus
    issue_locator: IssueLocator | None = None
    issue_ref: IssueRef | None = None
    finished_at: datetime | None = None
    duration_ns: int | None = None
    review_cycle: int | None = None
    provider_attempt: int | None = None
    terminal_outcome: RunOutcome | None = None
    git_baseline: GitState | None = None
    git_checks: tuple[GitCheckRecord, ...] = ()
    git_postflight: GitCheckRecord | None = None
    git_safety_status: GitSafetyStatus | None = None
    timeline: tuple[Mapping[str, FrozenJsonValue], ...] = ()
    attempts: tuple[AttemptRecord, ...] = ()
    errors: tuple[ErrorRecord, ...] = ()
    last_successful_sequence: int | None = None
    artifact_incomplete: bool = False
    trigger_outcome: RunOutcome | None = None
    final_status: FinalStatus | None = None
    expected_exit_code: int | None = None
    changes_preserved: bool = False
    termination_confirmed: bool | None = None

    def __post_init__(self) -> None:
        _require_int(self.schema_version, "schema_version", minimum=1)
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        _require_non_empty(self.run_id, "run_id")
        _require_path(self.artifact_path, "artifact_path", absolute=True)
        if not isinstance(self.workspace, Workspace):
            raise TypeError("workspace must be Workspace")
        if not isinstance(self.target, TargetRepository):
            raise TypeError("target must be TargetRepository")
        _require_int(self.issue_number, "issue_number", minimum=1)
        object.__setattr__(self, "config", _freeze_mapping(self.config, "config"))
        object.__setattr__(
            self,
            "environment",
            _freeze_mapping(self.environment, "environment"),
        )
        _require_utc(self.started_at, "started_at")
        _require_exact_enum(self.current_phase, PipelinePhase, "current_phase")
        _require_exact_enum(
            self.persistence_status,
            PersistenceStatus,
            "persistence_status",
        )

        if self.issue_locator is not None:
            if not isinstance(self.issue_locator, IssueLocator):
                raise TypeError("issue_locator must be IssueLocator or None")
            if self.issue_locator.number != self.issue_number:
                raise ValueError("issue_locator number must match issue_number")
        if self.issue_ref is not None:
            if not isinstance(self.issue_ref, IssueRef):
                raise TypeError("issue_ref must be IssueRef or None")
            if self.issue_locator is None:
                raise ValueError("issue_ref requires issue_locator")
            if self.issue_ref.locator != self.issue_locator:
                raise ValueError("issue_ref locator must match issue_locator")

        _require_optional_utc(self.finished_at, "finished_at")
        _require_optional_int(self.duration_ns, "duration_ns", minimum=0)
        _require_optional_int(self.review_cycle, "review_cycle", minimum=1)
        _require_optional_int(
            self.provider_attempt,
            "provider_attempt",
            minimum=1,
        )
        _require_optional_enum(
            self.terminal_outcome,
            RunOutcome,
            "terminal_outcome",
        )
        if self.git_baseline is not None and not isinstance(
            self.git_baseline,
            GitState,
        ):
            raise TypeError("git_baseline must be GitState or None")
        object.__setattr__(
            self,
            "git_checks",
            _copy_records(self.git_checks, GitCheckRecord, "git_checks"),
        )
        if self.git_postflight is not None and not isinstance(
            self.git_postflight,
            GitCheckRecord,
        ):
            raise TypeError("git_postflight must be GitCheckRecord or None")
        _require_optional_enum(
            self.git_safety_status,
            GitSafetyStatus,
            "git_safety_status",
        )
        object.__setattr__(
            self,
            "timeline",
            _copy_json_mappings(self.timeline, "timeline"),
        )
        object.__setattr__(
            self,
            "attempts",
            _copy_records(self.attempts, AttemptRecord, "attempts"),
        )
        object.__setattr__(
            self,
            "errors",
            _copy_records(self.errors, ErrorRecord, "errors"),
        )
        _require_optional_int(
            self.last_successful_sequence,
            "last_successful_sequence",
            minimum=0,
        )
        _require_bool(self.artifact_incomplete, "artifact_incomplete")
        _require_optional_enum(
            self.trigger_outcome,
            RunOutcome,
            "trigger_outcome",
        )
        _require_optional_enum(self.final_status, FinalStatus, "final_status")
        _require_optional_int(
            self.expected_exit_code,
            "expected_exit_code",
            minimum=0,
        )
        _require_bool(self.changes_preserved, "changes_preserved")
        _require_optional_bool(
            self.termination_confirmed,
            "termination_confirmed",
        )


@dataclass(frozen=True, slots=True)
class IssueResult:
    """The final, CLI-facing result returned by the orchestrator."""

    run_id: str
    artifact_path: Path
    final_status: FinalStatus
    expected_exit_code: int
    trigger_outcome: RunOutcome
    git_safety_status: GitSafetyStatus
    persistence_status: PersistenceStatus
    changes_preserved: bool
    termination_confirmed: bool | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.run_id, "run_id")
        _require_path(self.artifact_path, "artifact_path", absolute=True)
        _require_exact_enum(self.final_status, FinalStatus, "final_status")
        _require_int(
            self.expected_exit_code,
            "expected_exit_code",
            minimum=0,
        )
        _require_exact_enum(self.trigger_outcome, RunOutcome, "trigger_outcome")
        _require_exact_enum(
            self.git_safety_status,
            GitSafetyStatus,
            "git_safety_status",
        )
        _require_exact_enum(
            self.persistence_status,
            PersistenceStatus,
            "persistence_status",
        )
        _require_bool(self.changes_preserved, "changes_preserved")
        _require_optional_bool(
            self.termination_confirmed,
            "termination_confirmed",
        )


type PersistableDomainRecord = (
    Workspace
    | TargetRepository
    | RepositoryIdentity
    | IssueLocator
    | IssueRef
    | ProcessResult
    | ProviderDiagnostic
    | ParsedAgentResponse
    | AgentResult
    | GitState
    | GitCheckRecord
    | AttemptRecord
    | ErrorRecord
    | RunRecord
    | IssueResult
)
type DomainRecord = ProcessSpec | PersistableDomainRecord
type JsonConvertible = (
    JsonScalar
    | StrEnum
    | Path
    | datetime
    | PersistableDomainRecord
    | tuple[JsonConvertible, ...]
    | Mapping[str, JsonConvertible]
)

_SERIALIZABLE_RECORD_TYPES = (
    Workspace,
    TargetRepository,
    RepositoryIdentity,
    IssueLocator,
    IssueRef,
    ProcessResult,
    ProviderDiagnostic,
    ParsedAgentResponse,
    AgentResult,
    GitState,
    GitCheckRecord,
    AttemptRecord,
    ErrorRecord,
    RunRecord,
    IssueResult,
)


def _to_primitive(value: object) -> JsonValue:
    if isinstance(value, StrEnum):
        return value.value
    if value is None or type(value) in (bool, int, str):
        return cast(JsonScalar, value)
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("non-finite floats are not JSON values")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        _require_utc(value, "datetime")
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    if isinstance(value, ProcessSpec):
        raise TypeError("ProcessSpec contains runtime-only values")
    if isinstance(value, _SERIALIZABLE_RECORD_TYPES):
        return {
            item.name: _to_primitive(cast(object, getattr(value, item.name)))
            for item in fields(value)
        }
    if isinstance(value, (tuple, list)):
        return [
            _to_primitive(item)
            for item in cast(tuple[object, ...] | list[object], value)
        ]
    if isinstance(value, Mapping):
        primitive: dict[str, JsonValue] = {}
        for key, item in cast(Mapping[object, object], value).items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be strings")
            primitive[key] = _to_primitive(item)
        return primitive
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def to_primitive(value: JsonConvertible) -> JsonValue:
    """Return a detached tree containing only JSON-native primitive values.

    JSON text encoding, schema layout, parsing, and persistence deliberately
    remain outside this domain-only boundary.
    """

    return _to_primitive(value)


__all__ = (
    "AgentResult",
    "AgentRole",
    "AgentStatus",
    "AttemptRecord",
    "DomainRecord",
    "ErrorRecord",
    "FinalStatus",
    "FrozenJsonValue",
    "GitCheckRecord",
    "GitSafetyStatus",
    "GitState",
    "IssueLocator",
    "IssueRef",
    "IssueResult",
    "JsonConvertible",
    "JsonScalar",
    "JsonValue",
    "ParsedAgentResponse",
    "PersistableDomainRecord",
    "PersistenceStatus",
    "PipelinePhase",
    "ProcessResult",
    "ProcessSpec",
    "ProviderDiagnostic",
    "RepositoryIdentity",
    "ReviewStatus",
    "RunOutcome",
    "RunRecord",
    "RunRequest",
    "TargetRepository",
    "Workspace",
    "to_primitive",
)

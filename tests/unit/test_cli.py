"""Unit tests for the v0.1 CLI: parser/entrypoint/help (M14-01) and the
composition-root wiring (M14-02) -- built entirely from `ports.py` fakes,
never a real subprocess (that is `tests/component/test_cli_end_to_end.py`'s
job)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Self, cast

import pytest

from opencode_tools import coder_sandbox
from opencode_tools.cli import (
    _CaptureSink,
    _CliAgentRunner,
    _render_issue_result,
    _render_pre_init_failure,
    _render_preinit_interrupted,
    _SystemSleeper,
    _TeeSink,
    build_parser,
    main,
    parse_args,
    run_composed_pipeline,
)
from opencode_tools.domain import (
    AgentResult,
    AgentRole,
    AgentStatus,
    AppConfig,
    AttemptRecord,
    ConfigSource,
    ErrorRecord,
    ExecutionConfig,
    FinalStatus,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    IssueLocator,
    IssueRef,
    IssueResult,
    ParsedAgentResponse,
    PersistenceStatus,
    PipelinePhase,
    ProcessResult,
    ProcessSpec,
    ProviderDiagnostic,
    ProviderRetryConfig,
    RepositoryIdentity,
    RunOutcome,
    RunRecord,
    RunRequest,
    TargetRepository,
    Workspace,
)
from opencode_tools.errors import (
    ConfigError,
    LoggingError,
    OpenCodeToolsError,
    PreflightError,
    ProtocolError,
)
from opencode_tools.opencode import classify_provider_signal, open_run_capture_sink
from opencode_tools.ports import (
    AgentRunner,
    AttemptLogSink,
    GitSafetyPort,
    IssueResolver,
    LogChannel,
    OpenCodePreflightPort,
    ProcessRunner,
    RunStorePort,
    TargetLeaseFactory,
)
from opencode_tools.runlog import AttemptLogFileSink

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SOURCE_ROOT: Final = PROJECT_ROOT / "src"
MAIN_MODULE_PATH: Final = SOURCE_ROOT / "opencode_tools" / "__main__.py"

_VALID_ARGS: Final = (
    "run",
    "--workspace",
    "/tmp/workspace",
    "--target",
    ".",
    "--issue",
    "53",
)


# --- positive parsing ---------------------------------------------------


def test_parses_the_canonical_single_issue_shape() -> None:
    args = parse_args(_VALID_ARGS)

    assert args.command == "run"
    assert args.workspace == Path("/tmp/workspace")
    assert args.target == Path(".")
    assert args.issue == 53
    assert args.config is None


def test_target_accepts_a_nested_relative_path() -> None:
    args = parse_args((*_VALID_ARGS[:4], "backend/service", *_VALID_ARGS[5:]))

    assert args.target == Path("backend/service")


def test_config_is_optional_and_kept_as_a_plain_path() -> None:
    args = parse_args((*_VALID_ARGS, "--config", "custom.toml"))

    assert args.config == Path("custom.toml")


def test_main_composes_a_valid_parse_and_renders_a_pre_init_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A syntactically valid parse reaches composition: with a nonexistent
    `--workspace`, that fails at `ConfigError` before any run directory can
    exist (M14-03's pre-init contract) -- exit 10, no `FINAL_STATUS` line
    or artifact promise on stdout, and a stderr diagnosis naming the
    outcome and the sanitized `ConfigError` message."""

    exit_code = main(_VALID_ARGS)

    captured = capsys.readouterr()
    assert exit_code == 10
    assert captured.out == ""
    assert "FINAL_STATUS" not in captured.err
    assert "terminal outcome: CONFIG_ERROR" in captured.err
    assert "artifact: none" in captured.err


# --- negative parsing: rejected before any run ---------------------------


@pytest.mark.parametrize("issue_value", ["0", "-1", "1.5", "abc", "1,2,3", "1-5", ""])
def test_rejects_non_positive_or_non_scalar_issue_values(issue_value: str) -> None:
    argv = (*_VALID_ARGS[:6], issue_value)

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


def test_rejects_an_absolute_target() -> None:
    argv = (*_VALID_ARGS[:4], "/etc/passwd", *_VALID_ARGS[5:])

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "missing_flags",
    [
        ("--workspace", "/tmp/workspace", "--target", "."),
        ("--workspace", "/tmp/workspace", "--issue", "53"),
        ("--target", ".", "--issue", "53"),
        (),
    ],
)
def test_rejects_missing_required_arguments(missing_flags: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(("run", *missing_flags))

    assert exc_info.value.code == 2


def test_rejects_an_unknown_option() -> None:
    argv = (*_VALID_ARGS, "--model", "gpt-5")

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


def test_rejects_an_unknown_subcommand() -> None:
    argv = ("validate", "--workspace", "/tmp/workspace")

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


def test_rejects_a_batch_issue_form() -> None:
    argv = (*_VALID_ARGS, "54")

    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


def test_rejects_no_arguments_at_all() -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(())

    assert exc_info.value.code == 2


def test_a_syntax_error_prints_nothing_promising_an_artifact_or_marker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main((*_VALID_ARGS[:6], "-1"))

    captured = capsys.readouterr()
    assert "FINAL_STATUS" not in captured.out
    assert "FINAL_STATUS" not in captured.err
    assert "run.json" not in captured.out
    assert "run.json" not in captured.err


def test_a_syntax_error_never_reaches_main_return_value() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(("run", "--workspace", "/tmp/workspace"))

    assert exc_info.value.code == 2
    assert exc_info.value.code != 0


# --- help: documents only the v0.1 shape ----------------------------------


def test_top_level_help_lists_only_the_run_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(("--help",))

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "run" in out
    for future_subcommand in ("validate", "dry-run", "inspect"):
        assert future_subcommand not in out


def test_run_help_documents_exactly_the_four_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(("run", "--help"))

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for option in ("--workspace", "--target", "--issue", "--config"):
        assert option in out
    for forbidden in ("--format", "--json", "--interactive"):
        assert forbidden not in out


# --- __main__.py: delegates, no parsing or business logic of its own -----


def test_main_module_contains_no_parsing_or_business_logic() -> None:
    source = MAIN_MODULE_PATH.read_text(encoding="utf-8")

    assert "from opencode_tools.cli import main" in source
    assert "argparse" not in source
    assert "add_argument" not in source


def test_python_dash_m_invocation_exposes_the_same_run_interface() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SOURCE_ROOT)

    completed = subprocess.run(
        [sys.executable, "-m", "opencode_tools", "run", "--help"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    for option in ("--workspace", "--target", "--issue", "--config"):
        assert option in completed.stdout


def test_python_dash_m_invocation_rejects_a_syntax_error_with_exit_two() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SOURCE_ROOT)

    completed = subprocess.run(
        [sys.executable, "-m", "opencode_tools", "run", "--issue", "0"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "FINAL_STATUS" not in completed.stdout
    assert "FINAL_STATUS" not in completed.stderr


# --- pyproject.toml: canonical entry point --------------------------------


def test_pyproject_declares_the_canonical_console_script() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as manifest_file:
        manifest = cast(dict[str, object], tomllib.load(manifest_file))

    project = cast(dict[str, object], manifest["project"])
    scripts = cast(dict[str, object], project["scripts"])

    assert scripts == {"opencode-tools": "opencode_tools.cli:main"}


def test_build_parser_prog_matches_the_canonical_command_name() -> None:
    assert build_parser().prog == "opencode-tools"


# =========================================================================
# M14-02: composition-root wiring, built entirely from `ports.py` fakes.
# =========================================================================

NOW: Final = datetime(2026, 9, 15, 9, 0, 0, tzinfo=UTC)


class _RecordingSink:
    """A minimal `AttemptLogSink` fake that records writes."""

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


class _SteppingClock:
    def __init__(self, *, start: datetime = NOW) -> None:
        self._next = start

    def now(self) -> datetime:
        current = self._next
        self._next = current + timedelta(seconds=1)
        return current

    def monotonic_ns(self) -> int:
        raise AssertionError("not exercised by these tests")


class _RecordingSleeper:
    def sleep(self, seconds: float) -> None:
        raise AssertionError("not exercised by these tests")


def _workspace_and_target(tmp_path: Path) -> tuple[Workspace, TargetRepository]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace = Workspace(root=workspace_root)
    target = TargetRepository(
        root=workspace_root,
        workspace_relative=Path("."),
        git_common_dir=workspace_root / ".git",
    )
    return workspace, target


def _app_config(*, runtime_root: Path) -> AppConfig:
    return AppConfig(
        source=ConfigSource.DEFAULTS,
        execution=ExecutionConfig(
            opencode_timeout_seconds=30,
            utility_timeout_seconds=10,
            termination_grace_seconds=2,
            max_review_cycles=3,
        ),
        provider_retry=ProviderRetryConfig(
            max_attempts=3,
            initial_delay_seconds=1,
            multiplier=2.0,
            max_delay_seconds=30,
        ),
        runtime_root=runtime_root,
    )


def _git_state(*, target_root: Path, fingerprint: str = "fp-0") -> GitState:
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


def _git_check(
    *,
    sequence: int,
    purpose: str,
    state: GitState,
    safety_status: GitSafetyStatus = GitSafetyStatus.SAFE,
) -> GitCheckRecord:
    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=(),
        state=state,
        safety_status=safety_status,
    )


def _repository_identity() -> RepositoryIdentity:
    return RepositoryIdentity(
        host="github.com", owner="octocat", repository="hello-world", source="origin"
    )


def _issue_locator() -> IssueLocator:
    return IssueLocator(repository_identity=_repository_identity(), number=42)


class _FakeLease:
    def __init__(self) -> None:
        self.released = False
        self.quarantine_reasons: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.released = True

    def quarantine(self, reason: str) -> None:
        self.quarantine_reasons.append(reason)


class _FakeLeaseFactory:
    def __init__(self, lease: _FakeLease) -> None:
        self._lease = lease
        self.acquire_calls: list[tuple[Path, Path, str]] = []

    def acquire(self, target_root: Path, runtime_root: Path, run_id: str) -> _FakeLease:
        self.acquire_calls.append((target_root, runtime_root, run_id))
        return self._lease


class _FakeGitSafety:
    """`resolve_target` and `check_runtime_location` succeed; `check` hands
    back one clean `SAFE` checkpoint per call, in the fixed order bootstrap
    and a single failed architect invocation need (baseline, before, after).

    `accepted_fingerprint_after_purpose`, when given, simulates a role's
    own edit actually landing on the target's working tree: every `check`
    call up to and including the one whose `purpose` matches it returns
    `fingerprint="fp-0"`; every call after returns `fingerprint="fp-1"`
    instead. A `role=None` call (the baseline, or a postflight-style probe)
    additionally compares its own `baseline` argument against that current
    fingerprint and reports `UNSAFE` on a mismatch -- exactly the real
    `check_git_state` continuity check this fake otherwise always skips.
    """

    def __init__(
        self,
        *,
        target: TargetRepository,
        resolve_error: PreflightError | None = None,
        accepted_fingerprint_after_purpose: str | None = None,
    ) -> None:
        self._target = target
        self._resolve_error = resolve_error
        self._accepted_fingerprint_after_purpose = accepted_fingerprint_after_purpose
        self._fingerprint_changed = False
        self.check_calls: list[str] = []

    def check_runtime_location(self, runtime_root: Path) -> None:
        return None

    def resolve_target(
        self, workspace: Workspace, target_root: Path
    ) -> TargetRepository:
        if self._resolve_error is not None:
            raise self._resolve_error
        return self._target

    def check(
        self,
        target: TargetRepository,
        *,
        sequence: int,
        purpose: str,
        role: object | None = None,
        baseline: GitState | None = None,
    ) -> GitCheckRecord:
        self.check_calls.append(purpose)
        if purpose == self._accepted_fingerprint_after_purpose:
            self._fingerprint_changed = True
        current_fingerprint = "fp-1" if self._fingerprint_changed else "fp-0"
        safety_status = GitSafetyStatus.SAFE
        if (
            role is None
            and baseline is not None
            and baseline.fingerprint != current_fingerprint
        ):
            safety_status = GitSafetyStatus.UNSAFE
        return _git_check(
            sequence=sequence,
            purpose=purpose,
            state=_git_state(target_root=target.root, fingerprint=current_fingerprint),
            safety_status=safety_status,
        )


class _FakeIssueResolver:
    def resolve_repository(self, target: TargetRepository) -> RepositoryIdentity:
        return _repository_identity()

    def locate_issue(
        self, repository_identity: RepositoryIdentity, issue_number: int
    ) -> IssueLocator:
        return IssueLocator(
            repository_identity=repository_identity, number=issue_number
        )


class _FakeRunStore:
    def __init__(
        self,
        *,
        initialize_error: LoggingError | None = None,
        fail_persist_on_call: int | None = None,
        fail_persist_exactly_on_call: int | None = None,
        raise_on_persist_call: tuple[int, OpenCodeToolsError] | None = None,
    ) -> None:
        self._initialize_error = initialize_error
        self._fail_persist_on_call = fail_persist_on_call
        self._fail_persist_exactly_on_call = fail_persist_exactly_on_call
        self._raise_on_persist_call = raise_on_persist_call
        self.persist_calls = 0
        self.persisted_records: list[RunRecord] = []
        self._staged_final: RunRecord | None = None

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        if self._initialize_error is not None:
            raise self._initialize_error
        return workspace.root / ".opencode-tools" / "runs" / run_id

    def open_attempt_sink(
        self, role: object, review_cycle: int | None, provider_attempt: int
    ) -> _RecordingSink:
        return _RecordingSink(path=Path("attempt.log"))

    def _persist_attempt(self, record: RunRecord) -> PersistenceStatus:
        self.persist_calls += 1
        self.persisted_records.append(record)
        if (
            self._raise_on_persist_call is not None
            and self.persist_calls == self._raise_on_persist_call[0]
        ):
            raise self._raise_on_persist_call[1]
        if self.persist_calls == self._fail_persist_exactly_on_call:
            return PersistenceStatus.FAILED
        if (
            self._fail_persist_on_call is not None
            and self.persist_calls >= self._fail_persist_on_call
        ):
            return PersistenceStatus.FAILED
        return PersistenceStatus.OK

    def persist(self, record: object) -> PersistenceStatus:
        return self._persist_attempt(cast("RunRecord", record))

    def stage_final(self, record: RunRecord) -> PersistenceStatus:
        status = self._persist_attempt(record)
        self._staged_final = record if status is PersistenceStatus.OK else None
        return status

    def commit_final(self) -> PersistenceStatus:
        if self._staged_final is None:
            raise AssertionError("stage_final must precede commit_final")
        self._staged_final = None
        return PersistenceStatus.OK

    def abort_final(self) -> None:
        self._staged_final = None


class _FakeOpenCodePreflight:
    def verify(self) -> str:
        return "control-plane-digest"

    def recheck(self, expected_digest: str) -> None:
        return None


def _agent_process_result(
    *,
    workspace_root: Path,
    outcome: RunOutcome,
    termination_confirmed: bool | None = True,
) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/opencode", "run"),
        cwd=workspace_root,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        duration_ns=1_000_000_000,
        return_code=0 if outcome is RunOutcome.SUCCEEDED else 1,
        timed_out=False,
        termination_confirmed=termination_confirmed,
        log_path=Path("architect.log"),
        stdout_byte_count=0,
        stdout_sha256="digest",
        stderr_byte_count=0,
        stderr_sha256="digest",
        outcome=outcome,
    )


class _FakeAgentRunner:
    """Reports the architect `AGENT_STATUS: FAILED`, stopping the pipeline
    before the coder is ever invoked -- exercising the full bootstrap ->
    pipeline -> finalize composition without needing a 3-role choreography.
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        termination_confirmed: bool | None = True,
        process_outcome: RunOutcome | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._termination_confirmed = termination_confirmed
        self._process_outcome = process_outcome
        self.bind_issue_locator_calls: list[IssueLocator] = []
        self.run_calls: list[tuple[object, int | None, int]] = []

    def bind_issue_locator(self, issue_locator: IssueLocator) -> None:
        self.bind_issue_locator_calls.append(issue_locator)

    def run(
        self,
        role: object,
        prompt: str,
        workspace: Workspace,
        *,
        review_cycle: int | None,
        provider_attempt: int,
        sink: AttemptLogSink,
    ) -> object:

        self.run_calls.append((role, review_cycle, provider_attempt))
        outcome = self._process_outcome
        if outcome is None:
            # `ProcessResult` forbids an unconfirmed termination on a
            # `SUCCEEDED` outcome (domain.py's own invariant), so an
            # unconfirmed run here is reported as a `PROCESS_ERROR` instead
            # -- the realistic shape of a process group whose termination
            # could not be confirmed.
            outcome = (
                RunOutcome.SUCCEEDED
                if self._termination_confirmed is True
                else RunOutcome.PROCESS_ERROR
            )
        terminal_response = (
            ParsedAgentResponse(
                role=AgentRole.ARCHITECT,
                body="Could not access the issue.",
                agent_status=AgentStatus.FAILED,
            )
            if outcome is RunOutcome.SUCCEEDED
            else None
        )
        return AgentResult(
            role=AgentRole.ARCHITECT,
            phase=PipelinePhase.ARCHITECT,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            process=_agent_process_result(
                workspace_root=self._workspace_root,
                outcome=outcome,
                termination_confirmed=self._termination_confirmed,
            ),
            terminal_response=terminal_response,
            session_id="session-architect-1",
            verified_agent="architect",
            provider_diagnostic=None,
            outcome=(
                RunOutcome.AGENT_REPORTED_FAILURE
                if outcome is RunOutcome.SUCCEEDED
                else outcome
            ),
        )


class _SequencedAgentRunner:
    """An `AgentRunner` fake returning one pre-built `AgentResult` per call,
    in order -- for exercising more than one role/cycle in a single run
    (e.g. an architect that succeeds followed by a coder whose own attempt
    is what a test cares about)."""

    def __init__(self, results: list[AgentResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[object, int | None, int]] = []

    def run(
        self,
        role: object,
        prompt: str,
        workspace: Workspace,
        *,
        review_cycle: int | None,
        provider_attempt: int,
        sink: AttemptLogSink,
    ) -> object:
        self.calls.append((role, review_cycle, provider_attempt))
        return self._results.pop(0)


def test_pre_init_failure_returns_the_error_without_an_artifact(tmp_path: Path) -> None:
    """A `resolve_target` failure happens at bootstrap's very first step --
    before any lease, run directory, or `run.json` ever exists -- so
    `run_composed_pipeline` must return the raw error, never invoke the
    pipeline, and never claim a result."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    resolve_error = PreflightError("git_safety.dirty_worktree", "target is not clean")
    git_safety = _FakeGitSafety(target=target, resolve_error=resolve_error)
    run_store = _FakeRunStore()
    agent_runner = _FakeAgentRunner(workspace_root=workspace.root)

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", _FakeLeaseFactory(_FakeLease())),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert result is resolve_error
    assert run_store.persist_calls == 0
    assert agent_runner.run_calls == []
    assert agent_runner.bind_issue_locator_calls == []


def test_a_lease_acquired_before_an_early_failure_is_released_here(
    tmp_path: Path,
) -> None:
    """`bootstrap_run` never releases a lease it acquired itself when the
    failure happens too early to ever finalize (no run directory yet) --
    that is this composition root's own job (its docstring, and
    `BootstrapOutcome`'s)."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    git_safety = _FakeGitSafety(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    initialize_error = LoggingError("runlog.runtime_root_create_failed", "boom")
    run_store = _FakeRunStore(initialize_error=initialize_error)
    agent_runner = _FakeAgentRunner(workspace_root=workspace.root)

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert result is initialize_error
    assert lease_factory.acquire_calls
    assert lease.released is True
    assert agent_runner.run_calls == []


def test_a_full_bootstrap_binds_the_issue_locator_and_runs_the_pipeline(
    tmp_path: Path,
) -> None:
    """A clean bootstrap threads `IssueLocator` into an `AgentRunner` that
    accepts it, invokes exactly the architect (which reports `FAILED`), and
    converges through `finalize_run` into a `FAILED` `IssueResult` -- never
    a `FINAL_STATUS` marker or `run.json` promise of its own; this is pure
    composition wiring, proven with fakes only."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    git_safety = _FakeGitSafety(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    run_store = _FakeRunStore()
    agent_runner = _FakeAgentRunner(workspace_root=workspace.root)

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert agent_runner.bind_issue_locator_calls == [_issue_locator()]
    assert [call[0] for call in agent_runner.run_calls] == [AgentRole.ARCHITECT]
    assert isinstance(result, IssueResult)
    assert result.final_status is FinalStatus.FAILED
    assert result.trigger_outcome is RunOutcome.AGENT_REPORTED_FAILURE
    assert lease.released is True


def test_the_cancellation_handler_stays_installed_through_finalize_run(
    tmp_path: Path,
) -> None:
    """Issue #111 follow-up: on the ordinary success path (`run_issue_
    pipeline` returns without raising), `run_composed_pipeline` used to
    restore the previous SIGINT/SIGTERM handler in a `finally` attached
    only to the `try`/`except` around `run_issue_pipeline`, *before* the
    trailing `finalize_run` call that follows it -- reopening the exact
    post-bootstrap idle window the original fix closed. A SIGINT arriving
    during `finalize_run`'s own work (here: its postflight Git probe) would
    otherwise unwind as a raw `KeyboardInterrupt` past this function, caught
    only by `main`'s pre-init handler, even though the run had already
    finished. This sends a real SIGINT from inside a `GitSafetyPort.check`
    fake exactly when it is called for the `postflight` purpose -- i.e.
    strictly after `run_issue_pipeline` has already returned -- and asserts
    two things: `run_composed_pipeline` still returns a normal `IssueResult`
    instead of letting the signal escape, *and* the cancellation is actually
    recorded (`RunOutcome.INTERRUPTED`, exit code 20) rather than merely
    swallowed -- `finalize_run`'s own `late_cancellation_check` is what
    lets a signal arriving during its postflight probe still upgrade the
    `interrupted` decision it makes, instead of being silently absorbed
    once this handler stopped it from crashing the process outright."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    class _SigintDuringPostflight(_FakeGitSafety):
        def check(
            self,
            target: TargetRepository,
            *,
            sequence: int,
            purpose: str,
            role: object | None = None,
            baseline: GitState | None = None,
        ) -> GitCheckRecord:
            if purpose == "postflight":
                os.kill(os.getpid(), signal.SIGINT)
            return super().check(
                target,
                sequence=sequence,
                purpose=purpose,
                role=role,
                baseline=baseline,
            )

    git_safety = _SigintDuringPostflight(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    run_store = _FakeRunStore()
    # The default `_FakeAgentRunner` reports the architect `AGENT_STATUS:
    # FAILED`, so `run_issue_pipeline` returns normally (no exception) --
    # exactly the success path whose `finalize_run` call is at issue here.
    agent_runner = _FakeAgentRunner(workspace_root=workspace.root)

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert "postflight" in git_safety.check_calls
    assert isinstance(result, IssueResult)
    assert result.final_status is FinalStatus.FAILED
    assert result.trigger_outcome is RunOutcome.INTERRUPTED
    assert result.expected_exit_code == 20
    assert lease.released is True


def test_a_cancellation_after_the_terminal_attempts_own_agent_process_is_still_honored(
    tmp_path: Path,
) -> None:
    """Issue #111 follow-up: `_interrupted(pipeline_result)` only sees a
    live child's own `INTERRUPTED` process outcome -- it has no way to
    notice a cancellation requested *after* the terminal attempt's own
    agent process already returned (e.g. during that same attempt's
    after-attempt Git check, sink close, or `persist`), since `process.py`'s
    own per-subprocess handler has already been restored by then and this
    module's own persistent handler is the one active instead. Before this
    fix, such a cancellation was recorded on `IssueOrchestrator.
    cancellation_requested` but never consulted on the success path, so the
    run still converged as `AGENT_REPORTED_FAILURE` instead of
    `INTERRUPTED`. This sends a real SIGINT from inside a `GitSafetyPort.
    check` fake exactly when it is called for the architect's own
    after-attempt purpose -- strictly after the (default, `AGENT_STATUS:
    FAILED`) architect attempt has already returned, well before
    `finalize_run` is ever called -- and asserts the result is
    `RunOutcome.INTERRUPTED`, not the architect's own reported failure."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    class _SigintAfterTheAgentProcessReturns(_FakeGitSafety):
        def check(
            self,
            target: TargetRepository,
            *,
            sequence: int,
            purpose: str,
            role: object | None = None,
            baseline: GitState | None = None,
        ) -> GitCheckRecord:
            if purpose.endswith(":after"):
                os.kill(os.getpid(), signal.SIGINT)
            return super().check(
                target,
                sequence=sequence,
                purpose=purpose,
                role=role,
                baseline=baseline,
            )

    git_safety = _SigintAfterTheAgentProcessReturns(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    run_store = _FakeRunStore()
    agent_runner = _FakeAgentRunner(workspace_root=workspace.root)

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert any(call.endswith(":after") for call in git_safety.check_calls)
    assert isinstance(result, IssueResult)
    assert result.final_status is FinalStatus.FAILED
    assert result.trigger_outcome is RunOutcome.INTERRUPTED
    assert result.expected_exit_code == 20
    assert lease.released is True


def test_a_mid_pipeline_logging_error_still_converges_through_finalize_run(
    tmp_path: Path,
) -> None:
    """A `LoggingError` raised by `IssueOrchestrator.run_logical_invocation`
    after bootstrap has already persisted `run.json` (here: the architect's
    own post-attempt persist call) must not escape `run_composed_pipeline`
    uncaught -- `main` would otherwise mistake an already-initialized run
    for a pre-init failure (#109). It must converge through the same
    `finalize_run` every other terminal path does, still releasing the
    lease and still returning an `IssueResult`, never the raw error -- and
    the caught error itself must survive onto the persisted `RunRecord.
    errors`, exactly like `bootstrap_run`'s own late-stage except-handler
    already preserves its own caught error, so the terminal artifact and
    `_render_issue_result`'s stderr summary keep the actual diagnosis
    instead of only the bare `LOGGING_ERROR` outcome."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    git_safety = _FakeGitSafety(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    # Bootstrap persists twice (the initial record, then the PREFLIGHT ->
    # ARCHITECT-ready record) before any agent ever runs; failing the 3rd
    # call instead fails the architect's own post-attempt persist -- a
    # genuinely mid-pipeline failure, not a bootstrap one.
    run_store = _FakeRunStore(fail_persist_on_call=3)
    agent_runner = _FakeAgentRunner(workspace_root=workspace.root)

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert [call[0] for call in agent_runner.run_calls] == [AgentRole.ARCHITECT]
    assert isinstance(result, IssueResult)
    assert result.trigger_outcome is RunOutcome.LOGGING_ERROR
    assert result.final_status is FinalStatus.FAILED
    assert lease.released is True

    final_record = run_store.persisted_records[-1]
    assert [error.code for error in final_record.errors] == [
        "orchestrator.run_record_persist_failed"
    ]
    assert final_record.errors[-1].outcome is RunOutcome.LOGGING_ERROR
    assert final_record.errors[-1].phase is PipelinePhase.ARCHITECT


def test_a_mid_pipeline_logging_error_marks_the_artifact_incomplete_even_when_finalize_run_writes_cleanly(
    tmp_path: Path,
) -> None:
    """The architect's own post-attempt `persist` fails exactly once (a
    transient fault, e.g. `sink.close()` or `persist` itself); by the time
    `finalize_run` makes its own, separate write of `run.json`, the fault
    has cleared and that write succeeds. A persisted `run.json` this clean
    must still say the artifact is incomplete -- the interrupted attempt's
    own record never reached `record.attempts` -- rather than letting a
    merely-successful final write erase that fact: `_render_issue_result`'s
    incomplete-artifact warning depends on `IssueResult.persistence_status`
    staying non-`OK` here, not on `finalize_run`'s own write outcome."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    git_safety = _FakeGitSafety(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    # Bootstrap persists twice; the architect's own post-attempt persist
    # (call 3) fails exactly once and then clears -- `finalize_run`'s own
    # later write (call 4) is never made to fail.
    run_store = _FakeRunStore(fail_persist_exactly_on_call=3)
    agent_runner = _FakeAgentRunner(workspace_root=workspace.root)

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert run_store.persist_calls == 4
    assert isinstance(result, IssueResult)
    assert result.trigger_outcome is RunOutcome.LOGGING_ERROR
    # The point of the fix: `finalize_run`'s own write of `run.json`
    # succeeded (it is never made to fail here), yet the result must not
    # report a clean `OK` -- that would silently drop the incomplete-
    # artifact warning `_render_issue_result` owes the operator.
    assert result.persistence_status is PersistenceStatus.INCOMPLETE

    final_record = run_store.persisted_records[-1]
    assert final_record.persistence_status is PersistenceStatus.INCOMPLETE
    assert final_record.artifact_incomplete is True


def test_a_mid_pipeline_logging_error_still_quarantines_an_unconfirmed_termination(
    tmp_path: Path,
) -> None:
    """The architect's attempt itself observes `termination_confirmed=
    False` (a possibly still-live child) right before its own post-attempt
    `persist` call fails and raises `LoggingError` -- losing that
    observation to `IssueOrchestrator.record`, which never advances past
    the pre-attempt snapshot. `run_composed_pipeline`'s except-handler must
    still recover it via `IssueOrchestrator.last_observed_termination_
    confirmed` and pass `False` through to `finalize_run`, so postflight is
    forced `INDETERMINATE` and the lease is quarantined -- not silently
    released for another run to acquire -- exactly as it would have been
    had persistence never failed at all."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    git_safety = _FakeGitSafety(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    # Bootstrap persists twice before any agent ever runs; failing the 3rd
    # call fails the architect's own post-attempt persist instead.
    run_store = _FakeRunStore(fail_persist_on_call=3)
    agent_runner = _FakeAgentRunner(
        workspace_root=workspace.root, termination_confirmed=False
    )

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert [call[0] for call in agent_runner.run_calls] == [AgentRole.ARCHITECT]
    assert isinstance(result, IssueResult)
    assert result.termination_confirmed is False
    assert result.git_safety_status is GitSafetyStatus.INDETERMINATE
    assert result.final_status is FinalStatus.FAILED
    assert lease.quarantine_reasons
    assert lease.released is True


def test_an_observed_interruption_still_wins_over_an_unrelated_secondary_error(
    tmp_path: Path,
) -> None:
    """The architect's own attempt already observed `process.outcome is
    INTERRUPTED` before a *different*, later `OpenCodeToolsError` -- here a
    `ProtocolError` a scripted `run_store.persist` raises directly, standing
    in for whatever non-`LoggingError` an attempt's own tail could still
    raise -- is what `run_composed_pipeline` actually catches.
    `isinstance(error, RunInterruptedError)` alone is `False` for that
    error, so without `IssueOrchestrator.cancellation_requested` the
    already-observed interruption is lost and `resolve_terminal_outcome`
    reports the unrelated `PROTOCOL_ERROR` instead of `INTERRUPTED`."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    git_safety = _FakeGitSafety(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    # Bootstrap persists twice before any agent ever runs; the architect's
    # own post-attempt persist (call 3) raises directly instead of merely
    # reporting a failed status.
    secondary_error = ProtocolError(
        "opencode.export_agent_mismatch", "unrelated protocol failure"
    )
    run_store = _FakeRunStore(raise_on_persist_call=(3, secondary_error))
    agent_runner = _FakeAgentRunner(
        workspace_root=workspace.root,
        process_outcome=RunOutcome.INTERRUPTED,
        termination_confirmed=True,
    )

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert [call[0] for call in agent_runner.run_calls] == [AgentRole.ARCHITECT]
    assert isinstance(result, IssueResult)
    assert result.trigger_outcome is RunOutcome.INTERRUPTED
    assert result.final_status is FinalStatus.FAILED
    assert lease.released is True


def test_system_sleeper_cuts_a_long_sleep_short_once_cancellation_is_requested() -> (
    None
):
    """Issue #111 follow-up (Codex review on PR #128): a plain `time.sleep
    (seconds)` keeps running for its own full *remaining* duration even
    after this process's SIGINT/SIGTERM handler sets a cancellation flag
    and returns without raising -- CPython retries an interrupted blocking
    call with the recomputed timeout rather than abandoning it (PEP 475).
    Delegating an entire backoff delay -- up to `ProviderRetryConfig.
    max_delay_seconds`, 1800 seconds -- to one such call would mean a
    SIGINT arriving early in that delay has no visible effect for up to
    half an hour. `_SystemSleeper` must instead sleep in short polled
    increments so `request_cancellation` (called from a background thread
    here, standing in for the signal handler) can cut a 60-second sleep
    short almost immediately."""

    sleeper = _SystemSleeper()

    def _cancel_shortly() -> None:
        time.sleep(0.05)
        sleeper.request_cancellation()

    canceller = threading.Thread(target=_cancel_shortly, daemon=True)
    started = time.monotonic()
    canceller.start()
    sleeper.sleep(60.0)
    elapsed = time.monotonic() - started
    canceller.join(timeout=2.0)

    assert elapsed < 2.0


def test_system_sleeper_still_sleeps_the_full_duration_absent_cancellation() -> None:
    """The polled-increment implementation above must not shortchange an
    uncancelled sleep -- `run_provider_attempts`' own documented contract
    (System Design SS12.2) is that a retry's backoff is honored in full
    when nothing interrupts it."""

    sleeper = _SystemSleeper()

    started = time.monotonic()
    sleeper.sleep(0.3)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.3


def test_a_sigint_during_provider_retry_backoff_converges_through_finalize_run(
    tmp_path: Path,
) -> None:
    """Issue #111: unlike the "durante" case above (a live child's own
    `INTERRUPTED` process outcome), a SIGINT arriving during
    `run_provider_attempts`' own backoff sleep -- strictly between two
    provider attempts, with no subprocess alive at all -- used to reach
    Python's default disposition unhandled: `process.py`'s per-subprocess
    handler had already been torn down, and nothing else installed one, so
    it surfaced as a raw `KeyboardInterrupt` past `run_composed_pipeline`
    (which only catches `OpenCodeToolsError`) into `main`'s pre-init
    `except KeyboardInterrupt` -- misreporting an already-initialized run
    (`run.json` exists; the `IssueOrchestrator` is live) as having failed
    before it could ever start.

    `run_composed_pipeline` now installs a persistent SIGINT/SIGTERM
    handler, once `bootstrap_run` hands back the `IssueOrchestrator`, whose
    only job is `request_cancellation()`. This sends itself a real SIGINT
    from inside the injected `Sleeper.sleep()` -- exactly where the backoff
    delay would be spent -- to prove that *actual* handler is what is
    installed and fires, not a simulated flag flip. The architect's own
    retry attempt 2 is consequently never invoked: the next
    `run_logical_invocation` raises `RunInterruptedError` immediately, which
    the existing (#109) except-handler converges through the very same
    `finalize_run` any other terminal path uses -- `RunOutcome.INTERRUPTED`,
    exit code 20, and a released lease -- never `main`'s pre-init 130 path.
    """

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    git_safety = _FakeGitSafety(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    run_store = _FakeRunStore()

    # A trusted, budgeted, retryable `PROVIDER_ERROR` on attempt 1 -- the
    # underlying process itself terminated cleanly (`SUCCEEDED`); only the
    # provider diagnostic marks it retryable -- authorizes exactly one
    # retry (System Design SS12.2), which is what schedules the backoff
    # sleep this test's `Sleeper` fake hijacks.
    diagnostic = ProviderDiagnostic(
        source="opencode-stdout",
        signature="rate_limited",
        retryable=True,
        status_code=429,
    )
    first_attempt = AgentResult(
        role=AgentRole.ARCHITECT,
        phase=PipelinePhase.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        process=_agent_process_result(
            workspace_root=workspace.root, outcome=RunOutcome.SUCCEEDED
        ),
        terminal_response=None,
        session_id="session-architect-1",
        verified_agent="architect",
        provider_diagnostic=diagnostic,
        outcome=RunOutcome.PROVIDER_ERROR,
    )
    # A second element would mean the cancellation was *not* honored (the
    # retried attempt 2 ran anyway); `_SequencedAgentRunner.run` popping
    # from an exhausted list would itself fail the test loudly.
    agent_runner = _SequencedAgentRunner([first_attempt])

    class _SigintDuringBackoffSleep:
        """Sends this process a real SIGINT instead of actually sleeping --
        the same signal an operator's Ctrl-C would deliver mid-backoff."""

        def sleep(self, seconds: float) -> None:
            del seconds
            os.kill(os.getpid(), signal.SIGINT)

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_SigintDuringBackoffSleep(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert [call[0] for call in agent_runner.calls] == [AgentRole.ARCHITECT]
    assert isinstance(result, IssueResult)
    assert result.trigger_outcome is RunOutcome.INTERRUPTED
    assert result.expected_exit_code == 20
    assert result.final_status is FinalStatus.FAILED
    assert lease.released is True

    final_record = run_store.persisted_records[-1]
    assert final_record.errors[-1].outcome is RunOutcome.INTERRUPTED
    assert final_record.errors[-1].phase is PipelinePhase.ARCHITECT


def test_a_coder_persist_failure_is_tagged_with_the_coder_phase_not_the_architect(
    tmp_path: Path,
) -> None:
    """The architect's own attempt succeeds and persists (advancing
    `record` only as far as `PipelinePhase.ARCHITECT`); the *coder's* own
    post-attempt persist is what then fails. `record.current_phase` still
    names `ARCHITECT` -- the last role that happened to persist --  so the
    `ErrorRecord` this failure is folded into must instead be tagged via
    `IssueOrchestrator.last_attempted_phase`, which names the invocation
    actually failing: `CODER`."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    git_safety = _FakeGitSafety(target=target)
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    # Bootstrap persists twice, then the architect's own successful attempt
    # persists once more (call 3); failing the 4th call fails the coder's
    # own post-attempt persist instead.
    run_store = _FakeRunStore(fail_persist_on_call=4)

    issue_ref = IssueRef(
        locator=_issue_locator(),
        url="https://github.com/octocat/hello-world/issues/42",
        title="Fix the thing",
    )
    architect_result = AgentResult(
        role=AgentRole.ARCHITECT,
        phase=PipelinePhase.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        process=_agent_process_result(
            workspace_root=workspace.root, outcome=RunOutcome.SUCCEEDED
        ),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.ARCHITECT,
            body="Handoff.",
            agent_status=AgentStatus.READY,
            issue_ref=issue_ref,
        ),
        session_id="session-architect-1",
        verified_agent="architect",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )
    coder_result = AgentResult(
        role=AgentRole.CODER,
        phase=PipelinePhase.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(
            workspace_root=workspace.root, outcome=RunOutcome.SUCCEEDED
        ),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.CODER,
            body="Implemented the fix.",
            agent_status=AgentStatus.COMPLETED,
        ),
        session_id="session-coder-1-1",
        verified_agent="coder",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )
    agent_runner = _SequencedAgentRunner([architect_result, coder_result])

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
    ]
    assert isinstance(result, IssueResult)
    assert result.trigger_outcome is RunOutcome.LOGGING_ERROR

    final_record = run_store.persisted_records[-1]
    assert [error.code for error in final_record.errors] == [
        "orchestrator.run_record_persist_failed"
    ]
    assert final_record.errors[-1].phase is PipelinePhase.CODER


def test_a_coder_persist_failure_still_uses_the_coders_own_accepted_checkpoint(
    tmp_path: Path,
) -> None:
    """The coder's own attempt makes a permitted edit -- its `after` check
    is `SAFE` and advances `IssueOrchestrator`'s own live `_last_accepted_
    git_state` -- strictly *before* that same attempt's own post-attempt
    `persist` then fails. `record` never advances past the architect's own
    checkpoint, so deriving the postflight baseline from `record` alone
    would compare `finalize_run`'s postflight probe against the *pre-coder*
    state and falsely report the coder's already-accepted edit as `UNSAFE`.
    `IssueOrchestrator.last_accepted_git_state` must survive that loss."""

    workspace, target = _workspace_and_target(tmp_path)
    runtime_root = tmp_path / "runtime"
    app_config = _app_config(runtime_root=runtime_root)
    run_request = RunRequest(
        issue_number=42, workspace=workspace, target_root=workspace.root
    )

    # The coder's own `after` check (`CODER:1:1:after`) is where the edit
    # actually lands on the target's working tree; postflight's own probe
    # (`role=None`) then observes that same, already-landed state.
    git_safety = _FakeGitSafety(
        target=target, accepted_fingerprint_after_purpose="CODER:1:1:after"
    )
    lease = _FakeLease()
    lease_factory = _FakeLeaseFactory(lease)
    # Bootstrap persists twice, then the architect's own successful attempt
    # persists once more (call 3); failing the 4th call fails the coder's
    # own post-attempt persist instead.
    run_store = _FakeRunStore(fail_persist_on_call=4)

    issue_ref = IssueRef(
        locator=_issue_locator(),
        url="https://github.com/octocat/hello-world/issues/42",
        title="Fix the thing",
    )
    architect_result = AgentResult(
        role=AgentRole.ARCHITECT,
        phase=PipelinePhase.ARCHITECT,
        review_cycle=None,
        provider_attempt=1,
        process=_agent_process_result(
            workspace_root=workspace.root, outcome=RunOutcome.SUCCEEDED
        ),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.ARCHITECT,
            body="Handoff.",
            agent_status=AgentStatus.READY,
            issue_ref=issue_ref,
        ),
        session_id="session-architect-1",
        verified_agent="architect",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )
    coder_result = AgentResult(
        role=AgentRole.CODER,
        phase=PipelinePhase.CODER,
        review_cycle=1,
        provider_attempt=1,
        process=_agent_process_result(
            workspace_root=workspace.root, outcome=RunOutcome.SUCCEEDED
        ),
        terminal_response=ParsedAgentResponse(
            role=AgentRole.CODER,
            body="Implemented the fix.",
            agent_status=AgentStatus.COMPLETED,
        ),
        session_id="session-coder-1-1",
        verified_agent="coder",
        provider_diagnostic=None,
        outcome=RunOutcome.SUCCEEDED,
    )
    agent_runner = _SequencedAgentRunner([architect_result, coder_result])

    result = run_composed_pipeline(
        run_request=run_request,
        app_config=app_config,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        process_runner=cast("ProcessRunner", None),
        clock=_SteppingClock(),
        sleeper=_RecordingSleeper(),
        git_safety_port=cast("GitSafetyPort", git_safety),
        issue_resolver=cast("IssueResolver", _FakeIssueResolver()),
        run_store=cast("RunStorePort", run_store),
        lease_factory=cast("TargetLeaseFactory", lease_factory),
        opencode_preflight=cast("OpenCodePreflightPort", _FakeOpenCodePreflight()),
        agent_runner=cast("AgentRunner", agent_runner),
    )

    assert [call[0] for call in agent_runner.calls] == [
        AgentRole.ARCHITECT,
        AgentRole.CODER,
    ]
    assert isinstance(result, IssueResult)
    # The point of the fix: nothing changed *after* the coder's own already-
    # accepted edit, so postflight must still be `SAFE` -- comparing against
    # the stale, pre-coder checkpoint would report it `UNSAFE` instead.
    assert result.git_safety_status is GitSafetyStatus.SAFE


# --- _CliAgentRunner: decode/classify/parse/identity-verify composition --


class _ScriptedProcessRunner:
    """Replays one scripted `(stdout_bytes, ProcessResult)` pair per call,
    writing `stdout_bytes` into whatever sink it is given -- exactly what a
    real `SubprocessRunner` streaming a real child's output would produce."""

    def __init__(self, scripts: list[tuple[bytes, ProcessResult]]) -> None:
        self._scripts = list(scripts)
        self.specs: list[object] = []

    def run(self, spec: object, *, sink: AttemptLogSink) -> ProcessResult:
        self.specs.append(spec)
        stdout_bytes, result = self._scripts.pop(0)
        if stdout_bytes:
            sink.write("stdout", stdout_bytes, NOW)
        return result


def _process_result(
    *, stdout_bytes: bytes, outcome: RunOutcome, timed_out: bool = False
) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/opencode", "run"),
        cwd=Path("/workspace"),
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        duration_ns=1_000_000_000,
        return_code=0 if outcome is RunOutcome.SUCCEEDED else None,
        timed_out=timed_out,
        termination_confirmed=True,
        log_path=Path("architect.log"),
        stdout_byte_count=len(stdout_bytes),
        stdout_sha256=hashlib.sha256(stdout_bytes).hexdigest(),
        stderr_byte_count=0,
        stderr_sha256=hashlib.sha256(b"").hexdigest(),
        outcome=outcome,
    )


_ARCHITECT_READY_NDJSON = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "opencode"
    / "1.17.18"
    / "run"
    / "architect-ready-success.ndjson"
).read_bytes()
_ARCHITECT_EXPORT_JSON = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "opencode"
    / "1.17.18"
    / "export"
    / "architect-correct-agent.json"
).read_bytes()
_CODER_COMPLETED_NDJSON = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "opencode"
    / "1.17.18"
    / "run"
    / "coder-completed-success.ndjson"
).read_bytes()
_CODER_EXPORT_JSON = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "opencode"
    / "1.17.18"
    / "export"
    / "coder-correct-agent.json"
).read_bytes()


def _agent_runner(
    scripts: list[tuple[bytes, ProcessResult]],
    *,
    target_root: Path = Path("/workspace"),
) -> tuple[_CliAgentRunner, _ScriptedProcessRunner]:

    process_runner = _ScriptedProcessRunner(scripts)
    runner = _CliAgentRunner(
        cast("ProcessRunner", process_runner),
        executable=Path("/usr/bin/opencode"),
        target_root=target_root,
        opencode_timeout_seconds=30,
        utility_timeout_seconds=10,
        termination_grace_seconds=2,
        sandbox_coder=False,
    )
    runner.bind_issue_locator(_issue_locator())
    return runner, process_runner


def test_agent_runner_parses_a_successful_architect_response(tmp_path: Path) -> None:

    runner, _ = _agent_runner(
        [
            (
                _ARCHITECT_READY_NDJSON,
                _process_result(
                    stdout_bytes=_ARCHITECT_READY_NDJSON, outcome=RunOutcome.SUCCEEDED
                ),
            ),
            (
                _ARCHITECT_EXPORT_JSON,
                _process_result(
                    stdout_bytes=_ARCHITECT_EXPORT_JSON, outcome=RunOutcome.SUCCEEDED
                ),
            ),
        ]
    )
    workspace = Workspace(root=tmp_path)

    result = runner.run(
        AgentRole.ARCHITECT,
        "prompt",
        workspace,
        review_cycle=None,
        provider_attempt=1,
        sink=_RecordingSink(path=Path("architect.log")),
    )

    assert result.outcome is RunOutcome.SUCCEEDED
    assert result.terminal_response is not None
    assert result.terminal_response.agent_status is AgentStatus.READY
    assert result.session_id == "ses_architect_ready"
    assert result.verified_agent == "architect"
    assert result.provider_diagnostic is None
    assert result.identity_verification_error_code is None


def test_agent_runner_preserves_export_call_failure_code(tmp_path: Path) -> None:
    runner, process_runner = _agent_runner(
        [
            (
                _ARCHITECT_READY_NDJSON,
                _process_result(
                    stdout_bytes=_ARCHITECT_READY_NDJSON,
                    outcome=RunOutcome.SUCCEEDED,
                ),
            ),
            (
                b"",
                _process_result(stdout_bytes=b"", outcome=RunOutcome.PROCESS_ERROR),
            ),
        ]
    )

    result = runner.run(
        AgentRole.ARCHITECT,
        "prompt",
        Workspace(root=tmp_path),
        review_cycle=None,
        provider_attempt=1,
        sink=_RecordingSink(path=Path("architect.log")),
    )

    assert result.outcome is RunOutcome.PROTOCOL_ERROR
    assert result.terminal_response is None
    assert result.session_id == "ses_architect_ready"
    assert result.verified_agent is None
    assert result.provider_diagnostic is None
    assert result.identity_verification_error_code == "opencode.export_call_failed"
    assert len(process_runner.specs) == 2


@pytest.mark.parametrize(
    ("export_bytes", "expected_code"),
    [
        (
            b'{"sessionID":"ses_architect_ready"}',
            "opencode.export_invalid_schema",
        ),
        (
            b'{"messages":[{"info":{"role":"assistant"}}]}',
            "opencode.export_agent_missing",
        ),
        (
            b'{"messages":[{"info":{"role":"assistant","agent":"coder"}}]}',
            "opencode.export_agent_mismatch",
        ),
    ],
)
def test_agent_runner_preserves_identity_verification_failure_code(
    tmp_path: Path,
    export_bytes: bytes,
    expected_code: str,
) -> None:
    runner, process_runner = _agent_runner(
        [
            (
                _ARCHITECT_READY_NDJSON,
                _process_result(
                    stdout_bytes=_ARCHITECT_READY_NDJSON,
                    outcome=RunOutcome.SUCCEEDED,
                ),
            ),
            (
                export_bytes,
                _process_result(
                    stdout_bytes=export_bytes,
                    outcome=RunOutcome.SUCCEEDED,
                ),
            ),
        ]
    )

    result = runner.run(
        AgentRole.ARCHITECT,
        "prompt",
        Workspace(root=tmp_path),
        review_cycle=None,
        provider_attempt=1,
        sink=_RecordingSink(path=Path("architect.log")),
    )

    assert result.outcome is RunOutcome.PROTOCOL_ERROR
    assert result.terminal_response is None
    assert result.session_id == "ses_architect_ready"
    assert result.verified_agent is None
    assert result.provider_diagnostic is None
    assert result.identity_verification_error_code == expected_code
    assert len(process_runner.specs) == 2


def test_coder_promotion_failure_diagnostic_precedes_runner_footer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 on #133: promotion diagnostics are attempt-log writes too,
    so the runner footer must be emitted only after a blocked promotion has
    written its diagnostic and must remain the final record."""

    workspace, target = _workspace_and_target(tmp_path)
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    clock = _SteppingClock()
    process_runner = _ScriptedProcessRunner(
        [
            (
                _CODER_COMPLETED_NDJSON,
                _process_result(
                    stdout_bytes=_CODER_COMPLETED_NDJSON,
                    outcome=RunOutcome.SUCCEEDED,
                ),
            ),
            (
                _CODER_EXPORT_JSON,
                _process_result(
                    stdout_bytes=_CODER_EXPORT_JSON,
                    outcome=RunOutcome.SUCCEEDED,
                ),
            ),
        ]
    )

    class _Sandbox:
        root = sandbox_root

    sandbox = cast("coder_sandbox.CoderSandbox", _Sandbox())

    def _prepare(*args: object, **kwargs: object) -> coder_sandbox.CoderSandbox:
        del args, kwargs
        return sandbox

    def _promote(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise coder_sandbox.CoderSandboxError("simulated promotion failure")

    def _cleanup(value: coder_sandbox.CoderSandbox) -> None:
        assert value is sandbox

    monkeypatch.setattr(coder_sandbox, "prepare_coder_sandbox", _prepare)
    monkeypatch.setattr(coder_sandbox, "promote_coder_changes", _promote)
    monkeypatch.setattr(coder_sandbox, "cleanup_coder_sandbox", _cleanup)

    runner = _CliAgentRunner(
        cast("ProcessRunner", process_runner),
        executable=Path("/usr/bin/opencode"),
        target_root=target.root,
        opencode_timeout_seconds=30,
        utility_timeout_seconds=10,
        termination_grace_seconds=2,
        git_executable=Path("/usr/bin/git"),
        clock=clock,
        sandbox_coder=True,
    )
    runner.bind_issue_locator(_issue_locator())
    runner.bind_target(target)

    sink = AttemptLogFileSink(tmp_path, "coder.log", clock)
    result = runner.run(
        AgentRole.CODER,
        "prompt",
        workspace,
        review_cycle=1,
        provider_attempt=1,
        sink=sink,
    )
    sink.close()

    assert result.outcome is RunOutcome.PROTOCOL_ERROR
    records = [
        json.loads(line)
        for line in (tmp_path / "coder.log").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["channel"] == "runner"
    assert (
        json.loads(base64.b64decode(records[-1]["payload_base64"]))["event"] == "footer"
    )
    stderr_payloads = [
        base64.b64decode(record["payload_base64"])
        for record in records[:-1]
        if record["channel"] == "stderr"
    ]
    assert any(
        payload.startswith(
            b"coder sandbox promotion blocked: simulated promotion failure"
        )
        for payload in stderr_payloads
    )


def test_agent_runner_builds_direct_coder_spec_when_sandbox_is_disabled(
    tmp_path: Path,
) -> None:
    rate_limit_bytes = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "opencode"
        / "1.17.18"
        / "provider"
        / "http-429-rate-limit.ndjson"
    ).read_bytes()
    workspace = Workspace(root=tmp_path / "workspace")
    target_root = workspace.root / "Backend"
    runner, process_runner = _agent_runner(
        [
            (
                rate_limit_bytes,
                _process_result(
                    stdout_bytes=rate_limit_bytes, outcome=RunOutcome.SUCCEEDED
                ),
            )
        ],
        target_root=target_root,
    )

    runner.run(
        AgentRole.CODER,
        "prompt",
        workspace,
        review_cycle=1,
        provider_attempt=1,
        sink=_RecordingSink(path=Path("coder.log")),
    )

    spec = cast("ProcessSpec", process_runner.specs[0])
    assert spec.cwd == target_root
    assert spec.argv[-2:] == ("--dir", str(target_root))
    assert spec.environment_overrides["OPENCODE_CONFIG_DIR"] == str(
        workspace.root / ".opencode"
    )


def test_agent_runner_raises_when_issue_locator_is_not_bound(tmp_path: Path) -> None:

    process_runner = _ScriptedProcessRunner([])
    runner = _CliAgentRunner(
        cast("ProcessRunner", process_runner),
        executable=Path("/usr/bin/opencode"),
        target_root=tmp_path,
        opencode_timeout_seconds=30,
        utility_timeout_seconds=10,
        termination_grace_seconds=2,
        sandbox_coder=False,
    )
    workspace = Workspace(root=tmp_path)

    with pytest.raises(AssertionError):
        runner.run(
            AgentRole.ARCHITECT,
            "prompt",
            workspace,
            review_cycle=None,
            provider_attempt=1,
            sink=_RecordingSink(path=Path("architect.log")),
        )


def test_agent_runner_reports_a_provider_error_and_never_parses(tmp_path: Path) -> None:

    rate_limit_bytes = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "opencode"
        / "1.17.18"
        / "provider"
        / "http-429-rate-limit.ndjson"
    ).read_bytes()
    runner, process_runner = _agent_runner(
        [
            (
                rate_limit_bytes,
                _process_result(
                    stdout_bytes=rate_limit_bytes, outcome=RunOutcome.SUCCEEDED
                ),
            )
        ]
    )
    workspace = Workspace(root=tmp_path)

    result = runner.run(
        AgentRole.ARCHITECT,
        "prompt",
        workspace,
        review_cycle=None,
        provider_attempt=1,
        sink=_RecordingSink(path=Path("architect.log")),
    )

    assert result.provider_diagnostic is not None
    assert result.outcome is RunOutcome.PROVIDER_ERROR
    assert result.terminal_response is None
    assert result.session_id is None
    # A provider error is detected before ever attempting to decode the
    # transport, so no second (export/identity) subprocess call happens.
    assert len(process_runner.specs) == 1


def test_agent_runner_reports_the_nested_error_shape_as_a_provider_error(
    tmp_path: Path,
) -> None:
    """Regression for GitHub issue #80: the exact OpenCode 1.17.18 evidence
    from run `20260917T124944.374451Z-bfee6257f6f8` -- a top-level `error`
    event carrying a serialized-JSON Nvidia/OpenRouter overload payload,
    emitted after the coder had already edited the target -- must resolve
    to PROVIDER_ERROR, not PROCESS_ERROR with a null diagnostic. The
    underlying process outcome is itself PROCESS_ERROR here (the real
    `opencode` child also exited non-zero), so this proves the fixed
    classifier's PROVIDER_ERROR now outranks that lower-precedence signal
    (System Design SS13.2), matching the actual v0.1.0 regression -- not
    just that a diagnostic is present in isolation."""

    nested_error_bytes = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "opencode"
        / "1.17.18"
        / "provider"
        / "nested-error-provider-overloaded.ndjson"
    ).read_bytes()
    runner, process_runner = _agent_runner(
        [
            (
                nested_error_bytes,
                _process_result(
                    stdout_bytes=nested_error_bytes, outcome=RunOutcome.PROCESS_ERROR
                ),
            )
        ]
    )
    workspace = Workspace(root=tmp_path)

    result = runner.run(
        AgentRole.CODER,
        "prompt",
        workspace,
        review_cycle=1,
        provider_attempt=1,
        sink=_RecordingSink(path=Path("coder.log")),
    )

    assert result.provider_diagnostic is not None
    assert result.provider_diagnostic.source == "error"
    assert result.provider_diagnostic.signature == "overload"
    assert result.provider_diagnostic.status_code == 503
    assert result.provider_diagnostic.code == "provider_overloaded"
    assert result.provider_diagnostic.retryable is True
    assert result.outcome is RunOutcome.PROVIDER_ERROR
    assert result.terminal_response is None
    assert result.session_id is None
    assert len(process_runner.specs) == 1


def test_agent_runner_a_timeout_takes_precedence_over_everything_else(
    tmp_path: Path,
) -> None:

    runner, process_runner = _agent_runner(
        [
            (
                _ARCHITECT_READY_NDJSON,
                _process_result(
                    stdout_bytes=_ARCHITECT_READY_NDJSON,
                    outcome=RunOutcome.TIMEOUT,
                    timed_out=True,
                ),
            )
        ]
    )
    workspace = Workspace(root=tmp_path)

    result = runner.run(
        AgentRole.ARCHITECT,
        "prompt",
        workspace,
        review_cycle=None,
        provider_attempt=1,
        sink=_RecordingSink(path=Path("architect.log")),
    )

    assert result.process.timed_out is True
    assert result.outcome is RunOutcome.TIMEOUT
    assert result.terminal_response is None
    # Precedence is already decided by the timeout alone; this adapter
    # never spends a second (export/identity) subprocess call on it.
    assert len(process_runner.specs) == 1


def test_tee_sink_fans_writes_out_to_both_the_real_sink_and_the_capture() -> None:
    real_sink = _RecordingSink(path=Path("real.log"))

    capture = open_run_capture_sink("capture.log")
    tee = _TeeSink(cast("AttemptLogSink", real_sink), cast("_CaptureSink", capture))

    tee.write("stdout", b"hello", NOW)
    tee.close()

    assert real_sink.writes == [("stdout", b"hello", NOW)]
    assert real_sink.closed is True
    assert cast(_CaptureSink, capture).bytes_for("stdout") == b"hello"
    assert tee.path == Path("real.log")


# =========================================================================
# M14-03: final rendering, exit code, and stderr summary.
# =========================================================================


def _issue_result(
    *,
    final_status: FinalStatus = FinalStatus.FAILED,
    expected_exit_code: int = 20,
    trigger_outcome: RunOutcome = RunOutcome.AGENT_REPORTED_FAILURE,
    git_safety_status: GitSafetyStatus = GitSafetyStatus.SAFE,
    persistence_status: PersistenceStatus = PersistenceStatus.OK,
    changes_preserved: bool = True,
) -> IssueResult:
    return IssueResult(
        run_id="20260915T090000.000000Z-abcdefabcdef",
        artifact_path=Path(
            "/runtime/runs/20260915T090000.000000Z-abcdefabcdef/run.json"
        ),
        final_status=final_status,
        expected_exit_code=expected_exit_code,
        trigger_outcome=trigger_outcome,
        git_safety_status=git_safety_status,
        persistence_status=persistence_status,
        changes_preserved=changes_preserved,
    )


def _run_record(
    *,
    workspace: Workspace,
    target: TargetRepository,
    git_baseline: GitState | None = None,
    git_postflight: GitCheckRecord | None = None,
    errors: tuple[ErrorRecord, ...] = (),
    attempts: tuple[AttemptRecord, ...] = (),
) -> RunRecord:
    return RunRecord(
        schema_version=1,
        run_id="20260915T090000.000000Z-abcdefabcdef",
        artifact_path=workspace.root / ".opencode-tools" / "runs" / "r1" / "run.json",
        workspace=workspace,
        target=target,
        issue_number=42,
        config={},
        environment={},
        started_at=NOW,
        current_phase=PipelinePhase.FINISHED,
        persistence_status=PersistenceStatus.OK,
        git_baseline=git_baseline,
        git_postflight=git_postflight,
        errors=errors,
        attempts=attempts,
    )


_PHASE_BY_ROLE: dict[AgentRole, PipelinePhase] = {
    AgentRole.ARCHITECT: PipelinePhase.ARCHITECT,
    AgentRole.CODER: PipelinePhase.CODER,
    AgentRole.REVIEWER: PipelinePhase.REVIEWER,
}


def _attempt_record(
    *,
    target_root: Path,
    role: AgentRole = AgentRole.CODER,
    review_cycle: int | None = 1,
    provider_attempt: int = 1,
    terminal_response: ParsedAgentResponse | None = None,
    provider_diagnostic: ProviderDiagnostic | None = None,
) -> AttemptRecord:
    """Build a minimal, valid `AttemptRecord` for `_render_issue_result`
    tests -- only the two fields that drive the provider-diagnostic summary
    line (`terminal_response`, `provider_diagnostic`) vary; everything else
    is a plausible, structurally valid filler."""

    agent_result = AgentResult(
        role=role,
        phase=_PHASE_BY_ROLE[role],
        review_cycle=review_cycle,
        provider_attempt=provider_attempt,
        process=_process_result(stdout_bytes=b"", outcome=RunOutcome.SUCCEEDED),
        terminal_response=terminal_response,
        session_id=None,
        verified_agent=None,
        provider_diagnostic=provider_diagnostic,
        outcome=(
            RunOutcome.PROVIDER_ERROR
            if provider_diagnostic is not None
            else RunOutcome.SUCCEEDED
        ),
    )
    check = _git_check(
        sequence=0, purpose="check", state=_git_state(target_root=target_root)
    )
    return AttemptRecord(
        logical_invocation_id=f"run:{role.value}:{review_cycle or 0}",
        role=role,
        review_cycle=review_cycle,
        provider_attempt=provider_attempt,
        git_before=check,
        git_after=check,
        agent_result=agent_result,
        retry_decision=False,
    )


def _error_record(
    *, code: str, message: str, technical_detail: str | None = None
) -> ErrorRecord:
    return ErrorRecord(
        sequence=0,
        timestamp=NOW,
        phase=PipelinePhase.PREFLIGHT,
        outcome=RunOutcome.PREFLIGHT_ERROR,
        code=code,
        message=message,
        technical_detail=technical_detail,
    )


# --- _render_issue_result: the initialized-run terminal contract ---------


def test_render_issue_result_prints_exactly_one_final_status_line_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_issue_result(
        _issue_result(final_status=FinalStatus.APPROVED, expected_exit_code=0),
        last_record=None,
    )

    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == ["FINAL_STATUS: APPROVED"]


def test_render_issue_result_never_writes_final_status_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_issue_result(_issue_result(), last_record=None)

    assert "FINAL_STATUS" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("final_status", "trigger_outcome", "expected_exit_code"),
    [
        (FinalStatus.APPROVED, RunOutcome.SUCCEEDED, 0),
        (FinalStatus.FAILED, RunOutcome.PROVIDER_ERROR, 20),
        (FinalStatus.FAILED, RunOutcome.PROCESS_ERROR, 20),
        (FinalStatus.FAILED, RunOutcome.PROTOCOL_ERROR, 20),
        (FinalStatus.FAILED, RunOutcome.AGENT_REPORTED_FAILURE, 20),
        (FinalStatus.FAILED, RunOutcome.REVIEW_CYCLES_EXHAUSTED, 20),
        (FinalStatus.FAILED, RunOutcome.INTERRUPTED, 20),
        (FinalStatus.FAILED, RunOutcome.GIT_SAFETY_ERROR, 30),
        (FinalStatus.FAILED, RunOutcome.LOGGING_ERROR, 40),
    ],
)
def test_render_issue_result_returns_the_exit_code_issue_result_already_carries(
    final_status: FinalStatus, trigger_outcome: RunOutcome, expected_exit_code: int
) -> None:
    """`_render_issue_result` never recomputes precedence/the final gate --
    it returns `IssueResult.expected_exit_code` verbatim (System Design
    SS13.4's table, already resolved by `state_machine.resolve_exit_code`
    inside `finalize_run`)."""

    result = _issue_result(
        final_status=final_status,
        trigger_outcome=trigger_outcome,
        expected_exit_code=expected_exit_code,
        git_safety_status=(
            GitSafetyStatus.SAFE
            if trigger_outcome is not RunOutcome.GIT_SAFETY_ERROR
            else GitSafetyStatus.UNSAFE
        ),
        persistence_status=(
            PersistenceStatus.OK
            if trigger_outcome is not RunOutcome.LOGGING_ERROR
            else PersistenceStatus.FAILED
        ),
    )

    exit_code = _render_issue_result(result, last_record=None)

    assert exit_code == expected_exit_code


def test_render_issue_result_reports_run_id_phase_outcome_and_artifact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _issue_result(trigger_outcome=RunOutcome.AGENT_REPORTED_FAILURE)

    _render_issue_result(result, last_record=None)

    err = capsys.readouterr().err
    assert f"run_id: {result.run_id}" in err
    assert "phase: FINISHED" in err
    assert "terminal outcome: AGENT_REPORTED_FAILURE" in err
    assert f"artifact: {result.artifact_path}" in err


def test_render_issue_result_warns_when_final_persistence_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-046/SH-003: a logging failure must be surfaced explicitly as a
    possibly-incomplete artifact, never silently reported as a clean run."""

    result = _issue_result(
        trigger_outcome=RunOutcome.LOGGING_ERROR,
        expected_exit_code=40,
        persistence_status=PersistenceStatus.FAILED,
    )

    _render_issue_result(result, last_record=None)

    err = capsys.readouterr().err
    assert "may be incomplete" in err
    assert "final persistence failed" in err


def test_render_issue_result_distinguishes_an_incomplete_artifact_from_a_failed_final_write(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`PersistenceStatus.INCOMPLETE` means `run.json` itself was written
    successfully -- only an *earlier* attempt's own record or log never
    became durable -- unlike `PersistenceStatus.FAILED`, where this final
    write is what failed. Claiming "final persistence failed" for
    `INCOMPLETE` would be factually wrong and could make an operator
    distrust a terminal artifact that was, in fact, written cleanly."""

    result = _issue_result(
        trigger_outcome=RunOutcome.LOGGING_ERROR,
        expected_exit_code=40,
        persistence_status=PersistenceStatus.INCOMPLETE,
    )

    _render_issue_result(result, last_record=None)

    err = capsys.readouterr().err
    assert "incomplete" in err
    assert "final persistence failed" not in err


def test_render_issue_result_omits_the_change_summary_without_a_last_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_issue_result(_issue_result(), last_record=None)

    assert "changes" not in capsys.readouterr().err


def test_render_issue_result_groups_preserved_changes_by_staged_unstaged_untracked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SH-007/System Design SS13.4: counts and paths, grouped -- never diff
    content or quality -- read back from the last record `_CliRunStore`
    remembered persisting (never a second Git probe)."""

    workspace, target = _workspace_and_target(tmp_path)
    state = GitState(
        root=target.root,
        branch="main",
        head="deadbeef",
        porcelain_summary="M  a.py\n M b.py\n?? c.py",
        staged=("a.py",),
        unstaged=("b.py",),
        untracked=("c.py", "d.py"),
        fingerprint="fp-1",
    )
    record = _run_record(
        workspace=workspace,
        target=target,
        git_postflight=_git_check(sequence=1, purpose="postflight", state=state),
    )

    _render_issue_result(_issue_result(), last_record=record)

    err = capsys.readouterr().err
    assert "changes preserved: yes" in err
    assert "staged=1 unstaged=1 untracked=2" in err
    assert "staged: a.py" in err
    assert "unstaged (tracked): b.py" in err
    assert "untracked: c.py, d.py" in err


def test_render_issue_result_falls_back_to_the_baseline_state_without_postflight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that never reached a role's own checkpoint (e.g. it failed
    during issue resolution) still has a Git baseline on the record --
    `finalize_run` skips its own postflight probe only when there is no
    accepted checkpoint to compare against (System Design SS11.3)."""

    workspace, target = _workspace_and_target(tmp_path)
    baseline = _git_state(target_root=target.root, fingerprint="fp-baseline")
    record = _run_record(workspace=workspace, target=target, git_baseline=baseline)

    _render_issue_result(_issue_result(), last_record=record)

    assert "staged=0 unstaged=0 untracked=0" in capsys.readouterr().err


def test_render_issue_result_surfaces_the_persisted_error_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compatibility diagnosis (a version mismatch discovered during
    `bootstrap_run`'s own OpenCode preflight, step 6) reaches the CLI only
    through the last persisted `RunRecord.errors` -- `IssueResult` itself
    carries no error detail, only `trigger_outcome`."""

    workspace, target = _workspace_and_target(tmp_path)
    record = _run_record(
        workspace=workspace,
        target=target,
        errors=(
            _error_record(
                code="opencode.version_mismatch",
                message=(
                    "OpenCode reported a version other than the exact "
                    "candidate 1.17.18."
                ),
                technical_detail="reported='1.17.17'",
            ),
        ),
    )

    _render_issue_result(_issue_result(), last_record=record)

    err = capsys.readouterr().err
    assert "opencode.version_mismatch" in err
    assert "candidate 1.17.18" in err
    assert "reported='1.17.17'" in err


# --- _render_issue_result: the last attempt's provider diagnostic (#80) --


def test_render_issue_result_surfaces_the_last_attempts_provider_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for GitHub issue #80's remaining acceptance criterion:
    when the last attempt left no terminal agent response, its
    `ProviderDiagnostic` -- here the real classifier's own output for the
    genuine nested top-level `error` event, not a synthetic one -- must
    reach the operator through the final stderr summary."""

    nested_error_text = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "opencode"
        / "1.17.18"
        / "provider"
        / "nested-error-provider-overloaded.ndjson"
    ).read_text(encoding="utf-8")
    diagnostic = classify_provider_signal(nested_error_text)
    assert diagnostic is not None

    workspace, target = _workspace_and_target(tmp_path)
    record = _run_record(
        workspace=workspace,
        target=target,
        attempts=(
            _attempt_record(
                target_root=target.root,
                terminal_response=None,
                provider_diagnostic=diagnostic,
            ),
        ),
    )

    _render_issue_result(
        _issue_result(trigger_outcome=RunOutcome.PROVIDER_ERROR), last_record=record
    )

    err = capsys.readouterr().err
    assert "provider diagnostic: overload" in err
    assert "source=error" in err
    assert "code=provider_overloaded" in err
    assert "status=503" in err


def test_render_issue_result_omits_the_provider_diagnostic_line_without_attempts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace, target = _workspace_and_target(tmp_path)
    record = _run_record(workspace=workspace, target=target)

    _render_issue_result(_issue_result(), last_record=record)

    assert "provider diagnostic" not in capsys.readouterr().err


def test_render_issue_result_omits_the_provider_diagnostic_line_without_a_last_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_issue_result(_issue_result(), last_record=None)

    assert "provider diagnostic" not in capsys.readouterr().err


def test_render_issue_result_omits_the_provider_diagnostic_line_when_a_terminal_response_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate is "no terminal agent response", read directly off the
    persisted attempt -- not merely "a provider_diagnostic is present" --
    so a diagnostic accompanied by a terminal response is never surfaced
    here (never recomputed precedence: a plain, defensive field read)."""

    workspace, target = _workspace_and_target(tmp_path)
    record = _run_record(
        workspace=workspace,
        target=target,
        attempts=(
            _attempt_record(
                target_root=target.root,
                terminal_response=ParsedAgentResponse(
                    role=AgentRole.CODER,
                    body="done",
                    agent_status=AgentStatus.COMPLETED,
                ),
                provider_diagnostic=ProviderDiagnostic(
                    source="session.error", signature="429", retryable=True
                ),
            ),
        ),
    )

    _render_issue_result(_issue_result(), last_record=record)

    assert "provider diagnostic" not in capsys.readouterr().err


def test_render_issue_result_uses_only_the_most_recent_attempts_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A provider error retried and then recovered must not still show the
    stale diagnostic from the earlier, non-final attempt -- only the last
    `AttemptRecord` is ever consulted, matching `_render_issue_result`'s own
    reliance on the pipeline's already-decided terminal state."""

    workspace, target = _workspace_and_target(tmp_path)
    record = _run_record(
        workspace=workspace,
        target=target,
        attempts=(
            _attempt_record(
                target_root=target.root,
                provider_attempt=1,
                terminal_response=None,
                provider_diagnostic=ProviderDiagnostic(
                    source="session.error", signature="429", retryable=True
                ),
            ),
            _attempt_record(
                target_root=target.root,
                provider_attempt=2,
                terminal_response=ParsedAgentResponse(
                    role=AgentRole.CODER,
                    body="done",
                    agent_status=AgentStatus.COMPLETED,
                ),
                provider_diagnostic=None,
            ),
        ),
    )

    _render_issue_result(
        _issue_result(final_status=FinalStatus.APPROVED, expected_exit_code=0),
        last_record=record,
    )

    assert "provider diagnostic" not in capsys.readouterr().err


def test_render_issue_result_keeps_the_stdout_contract_with_a_provider_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The provider-diagnostic line is stderr-only: stdout must still carry
    exactly the one `FINAL_STATUS` line and nothing else (System Design
    SS13.4's stdout contract is unaffected by this addition)."""

    workspace, target = _workspace_and_target(tmp_path)
    record = _run_record(
        workspace=workspace,
        target=target,
        attempts=(
            _attempt_record(
                target_root=target.root,
                terminal_response=None,
                provider_diagnostic=ProviderDiagnostic(
                    source="error",
                    signature="overload",
                    retryable=True,
                    status_code=503,
                    code="provider_overloaded",
                ),
            ),
        ),
    )

    _render_issue_result(
        _issue_result(final_status=FinalStatus.FAILED, expected_exit_code=20),
        last_record=record,
    )

    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["FINAL_STATUS: FAILED"]
    assert "provider diagnostic: overload" in captured.err


# --- _render_pre_init_failure: no artifact/FINAL_STATUS promised ---------


def test_render_pre_init_failure_prints_nothing_promising_an_artifact_or_marker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = ConfigError("config.workspace_not_found", "workspace does not exist")

    exit_code = _render_pre_init_failure(error)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "FINAL_STATUS" not in captured.err
    assert "run.json" not in captured.err
    assert "artifact: none" in captured.err
    assert exit_code == 10


@pytest.mark.parametrize(
    ("error_type", "code", "expected_exit_code"),
    [
        (ConfigError, "config.workspace_not_found", 10),
        (PreflightError, "git_safety.dirty_worktree", 10),
    ],
)
def test_render_pre_init_failure_maps_the_pre_init_outcome_family(
    error_type: type[ConfigError | PreflightError],
    code: str,
    expected_exit_code: int,
) -> None:
    """Only `CONFIG_ERROR`/`PREFLIGHT_ERROR` are reachable before a run
    directory can exist (System Design SS8.2's bootstrap ordering); both
    fall in the 10 family."""

    error = error_type(code, "a pre-init failure")

    exit_code = _render_pre_init_failure(error)

    assert exit_code == expected_exit_code


def test_render_pre_init_failure_names_the_detected_version_and_baseline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SH-005/the issue's own AC: a compatibility failure names the
    detected version, the supported baseline, and remediation -- all
    already sanitized by `opencode.py`'s own `PreflightError`, never
    fabricated here."""

    error = PreflightError(
        "opencode.version_mismatch",
        "OpenCode reported a version other than the exact candidate 1.17.18.",
        technical_detail="reported='1.17.17'",
    )

    _render_pre_init_failure(error)

    err = capsys.readouterr().err
    assert "candidate 1.17.18" in err
    assert "reported='1.17.17'" in err


def test_render_pre_init_failure_renders_every_cause_earliest_first(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root_cause = LoggingError("runlog.runtime_root_create_failed", "disk full")
    wrapping_error = PreflightError(
        "opencode.preflight_aborted",
        "preflight could not proceed",
        causes=(root_cause,),
    )

    _render_pre_init_failure(wrapping_error)

    err = capsys.readouterr().err
    assert err.index("runlog.runtime_root_create_failed") < err.index(
        "opencode.preflight_aborted"
    )


def test_render_preinit_interrupted_returns_130_and_promises_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = _render_preinit_interrupted()

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.out == ""
    assert "FINAL_STATUS" not in captured.err
    assert "run.json" not in captured.err


# --- main(): wiring the renderers into the real entrypoint ---------------


def test_main_returns_130_on_a_sigint_before_the_run_could_be_initialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `KeyboardInterrupt` reaching `main` can only ever originate before a
    run directory exists: this one is raised by `runlog.check_platform_
    baseline` itself, strictly before `run_composed_pipeline` -- and so
    before `bootstrap_run` -- is ever even called, so no persistent
    cancellation handler (issue #111) has been installed yet either. Once a
    run directory exists, `run_composed_pipeline`'s own persistent SIGINT/
    SIGTERM handler and `process.py`'s per-subprocess one between them turn
    a SIGINT into `RunOutcome.INTERRUPTED` on the pipeline itself, never a
    raw Python exception reaching this far -- so `main` maps *this*,
    genuinely pre-init case to exit 130 with no `FINAL_STATUS` promise, per
    FR-047 and System Design SS13.4."""

    from opencode_tools import runlog

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def _raise_keyboard_interrupt() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(runlog, "check_platform_baseline", _raise_keyboard_interrupt)

    exit_code = main((*_VALID_ARGS[:2], str(workspace), *_VALID_ARGS[3:]))

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.out == ""
    assert "FINAL_STATUS" not in captured.err

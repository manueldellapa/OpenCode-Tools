"""Unit tests for the v0.1 CLI: parser/entrypoint/help (M14-01) and the
composition-root wiring (M14-02) -- built entirely from `ports.py` fakes,
never a real subprocess (that is `tests/component/test_cli_end_to_end.py`'s
job)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Self, cast

import pytest

from opencode_tools.cli import (
    _CaptureSink,
    _CliAgentRunner,
    _render_issue_result,
    _render_pre_init_failure,
    _render_preinit_interrupted,
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
    ConfigSource,
    ErrorRecord,
    ExecutionConfig,
    FinalStatus,
    GitCheckRecord,
    GitSafetyStatus,
    GitState,
    IssueLocator,
    IssueResult,
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
from opencode_tools.errors import ConfigError, LoggingError, PreflightError
from opencode_tools.opencode import open_run_capture_sink
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


def _git_check(*, sequence: int, purpose: str, state: GitState) -> GitCheckRecord:
    return GitCheckRecord(
        sequence=sequence,
        purpose=purpose,
        process_results=(),
        state=state,
        safety_status=GitSafetyStatus.SAFE,
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
    """

    def __init__(
        self, *, target: TargetRepository, resolve_error: PreflightError | None = None
    ) -> None:
        self._target = target
        self._resolve_error = resolve_error
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
        return _git_check(
            sequence=sequence,
            purpose=purpose,
            state=_git_state(target_root=target.root),
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
    def __init__(self, *, initialize_error: LoggingError | None = None) -> None:
        self._initialize_error = initialize_error
        self.persist_calls = 0

    def initialize(self, workspace: Workspace, run_id: str) -> Path:
        if self._initialize_error is not None:
            raise self._initialize_error
        return workspace.root / ".opencode-tools" / "runs" / run_id

    def open_attempt_sink(
        self, role: object, review_cycle: int | None, provider_attempt: int
    ) -> _RecordingSink:
        return _RecordingSink(path=Path("attempt.log"))

    def persist(self, record: object) -> PersistenceStatus:
        self.persist_calls += 1
        return PersistenceStatus.OK


class _FakeOpenCodePreflight:
    def verify(self) -> str:
        return "control-plane-digest"

    def recheck(self, expected_digest: str) -> None:
        return None


def _agent_process_result(
    *, workspace_root: Path, outcome: RunOutcome
) -> ProcessResult:
    return ProcessResult(
        command=("/usr/bin/opencode", "run"),
        cwd=workspace_root,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        duration_ns=1_000_000_000,
        return_code=0 if outcome is RunOutcome.SUCCEEDED else 1,
        timed_out=False,
        termination_confirmed=True,
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

    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
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
        response = ParsedAgentResponse(
            role=AgentRole.ARCHITECT,
            body="Could not access the issue.",
            agent_status=AgentStatus.FAILED,
        )
        return AgentResult(
            role=AgentRole.ARCHITECT,
            phase=PipelinePhase.ARCHITECT,
            review_cycle=review_cycle,
            provider_attempt=provider_attempt,
            process=_agent_process_result(
                workspace_root=self._workspace_root, outcome=RunOutcome.SUCCEEDED
            ),
            terminal_response=response,
            session_id="session-architect-1",
            verified_agent="architect",
            provider_diagnostic=None,
            outcome=RunOutcome.AGENT_REPORTED_FAILURE,
        )


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


def _agent_runner(
    scripts: list[tuple[bytes, ProcessResult]],
) -> tuple[_CliAgentRunner, _ScriptedProcessRunner]:

    process_runner = _ScriptedProcessRunner(scripts)
    runner = _CliAgentRunner(
        cast("ProcessRunner", process_runner),
        executable=Path("/usr/bin/opencode"),
        opencode_timeout_seconds=30,
        utility_timeout_seconds=10,
        termination_grace_seconds=2,
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


def test_agent_runner_raises_when_issue_locator_is_not_bound(tmp_path: Path) -> None:

    process_runner = _ScriptedProcessRunner([])
    runner = _CliAgentRunner(
        cast("ProcessRunner", process_runner),
        executable=Path("/usr/bin/opencode"),
        opencode_timeout_seconds=30,
        utility_timeout_seconds=10,
        termination_grace_seconds=2,
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

    assert "may be incomplete" in capsys.readouterr().err


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
    run directory exists -- once one exists, `process.py`'s own signal
    handling (installed only around a live subprocess) turns a SIGINT into
    `RunOutcome.INTERRUPTED` on the pipeline itself, never a raw Python
    exception -- so `main` maps it to exit 130 with no `FINAL_STATUS`
    promise, per FR-047 and System Design SS13.4."""

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

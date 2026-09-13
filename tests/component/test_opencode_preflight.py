"""Component tests for the OpenCode `1.17.18` preflight sequence (M07-02).

Spawns `tests/component/helpers/fake_opencode.py` through the real
`SubprocessRunner`, never a fixture or mock of `ProcessRunner` itself, to
prove `run_preflight`/`recheck_control_plane` build the real argv sequence
ADR-005 SS10.4 fixes and fail closed on every scenario System Design SS20.4
requires evidence for: call count, an unknown version, a missing capability,
an agent that is missing/subagent/fallback, a permission-policy mismatch,
and control-plane drift before the next run.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import Workspace
from opencode_tools.errors import PreflightError, ProtocolError
from opencode_tools.opencode import (
    ControlPlaneEvidence,
    recheck_control_plane,
    run_preflight,
)
from opencode_tools.process import SubprocessRunner

HELPER = Path(__file__).resolve().parent / "helpers" / "fake_opencode.py"
FIXTURES_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "opencode" / "1.17.18"
)
DEBUG_FIXTURES = FIXTURES_ROOT / "debug"

UTILITY_TIMEOUT_SECONDS = 5.0
TERMINATION_GRACE_SECONDS = 1.0


class RealClock:
    """A `Clock` reading genuine wall/monotonic time for a real subprocess."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(root=tmp_path)


def _preflight(tmp_path: Path) -> ControlPlaneEvidence:
    return run_preflight(
        SubprocessRunner(RealClock()),
        executable=HELPER,
        workspace=_workspace(tmp_path),
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


def _set_baseline_debug_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_CONFIG_FILE", str(DEBUG_FIXTURES / "config-baseline.json")
    )
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_ARCHITECT_FILE",
        str(DEBUG_FIXTURES / "agent-architect-baseline.json"),
    )
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_CODER_FILE",
        str(DEBUG_FIXTURES / "agent-coder-baseline.json"),
    )
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_REVIEWER_FILE",
        str(DEBUG_FIXTURES / "agent-reviewer-baseline.json"),
    )


# --- happy path: call count and order ----------------------------------------


def test_run_preflight_calls_each_endpoint_exactly_once_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_OPENCODE_CALL_LOG_FILE", str(call_log))
    _set_baseline_debug_fixtures(monkeypatch)

    evidence = _preflight(tmp_path)

    assert evidence.version == "1.17.18"
    assert evidence.executable == HELPER
    assert evidence.control_plane_digest

    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        "--version",
        "run --help",
        "debug config",
        "debug agent architect",
        "debug agent coder",
        "debug agent reviewer",
    ]


# --- unknown version ----------------------------------------------------------


def test_run_preflight_fails_closed_on_an_unknown_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OPENCODE_VERSION", "1.17.19")

    with pytest.raises(PreflightError) as exc_info:
        _preflight(tmp_path)
    assert exc_info.value.code == "opencode.version_mismatch"


# --- missing capability --------------------------------------------------------


def test_run_preflight_fails_closed_on_a_missing_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OPENCODE_RUN_HELP", "Usage: opencode run [--agent <name>]")

    with pytest.raises(PreflightError) as exc_info:
        _preflight(tmp_path)
    assert exc_info.value.code == "opencode.capability_missing"


# --- agent missing/subagent/fallback -------------------------------------------


def test_run_preflight_fails_closed_when_an_agent_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_baseline_debug_fixtures(monkeypatch)
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_ARCHITECT_FILE",
        str(DEBUG_FIXTURES / "agent-missing.json"),
    )

    with pytest.raises(PreflightError) as exc_info:
        _preflight(tmp_path)
    assert exc_info.value.code == "opencode.debug_agent_identity_mismatch"


def test_run_preflight_fails_closed_on_a_subagent_mode_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_baseline_debug_fixtures(monkeypatch)
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_REVIEWER_FILE",
        str(DEBUG_FIXTURES / "agent-reviewer-subagent-mode.json"),
    )

    with pytest.raises(PreflightError) as exc_info:
        _preflight(tmp_path)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


def test_run_preflight_fails_closed_on_a_silent_fallback_to_another_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback_file = tmp_path / "agent-fallback.json"
    fallback_file.write_text(
        '{"name": "general", "mode": "primary", '
        '"tools": {"ask": false, "task": false}, '
        '"permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"}}',
        encoding="utf-8",
    )
    _set_baseline_debug_fixtures(monkeypatch)
    monkeypatch.setenv("FAKE_OPENCODE_DEBUG_AGENT_ARCHITECT_FILE", str(fallback_file))

    with pytest.raises(PreflightError) as exc_info:
        _preflight(tmp_path)
    assert exc_info.value.code == "opencode.debug_agent_identity_mismatch"


# --- permission-policy mismatch -------------------------------------------------


def test_run_preflight_fails_closed_on_a_permission_policy_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_baseline_debug_fixtures(monkeypatch)
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_ARCHITECT_FILE",
        str(DEBUG_FIXTURES / "agent-architect-permissive-edit.json"),
    )

    with pytest.raises(PreflightError) as exc_info:
        _preflight(tmp_path)
    assert exc_info.value.code == "opencode.debug_agent_rejected"


def test_run_preflight_fails_closed_on_automatic_sharing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_baseline_debug_fixtures(monkeypatch)
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_CONFIG_FILE",
        str(DEBUG_FIXTURES / "config-auto-share-enabled.json"),
    )

    with pytest.raises(PreflightError) as exc_info:
        _preflight(tmp_path)
    assert exc_info.value.code == "opencode.debug_config_rejected"


# --- control-plane digest drift -------------------------------------------------


def test_recheck_control_plane_passes_when_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_baseline_debug_fixtures(monkeypatch)
    evidence = _preflight(tmp_path)

    recheck_control_plane(
        SubprocessRunner(RealClock()),
        executable=HELPER,
        workspace=_workspace(tmp_path),
        utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
        expected_digest=evidence.control_plane_digest,
    )


def test_recheck_control_plane_detects_drift_before_the_next_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_baseline_debug_fixtures(monkeypatch)
    evidence = _preflight(tmp_path)

    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_CONFIG_FILE",
        str(DEBUG_FIXTURES / "config-auto-share-enabled.json"),
    )

    with pytest.raises(ProtocolError) as exc_info:
        recheck_control_plane(
            SubprocessRunner(RealClock()),
            executable=HELPER,
            workspace=_workspace(tmp_path),
            utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
            termination_grace_seconds=TERMINATION_GRACE_SECONDS,
            expected_digest=evidence.control_plane_digest,
        )
    assert exc_info.value.code == "opencode.control_plane_drift"


def test_recheck_control_plane_surfaces_a_call_failure_as_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_baseline_debug_fixtures(monkeypatch)
    evidence = _preflight(tmp_path)

    monkeypatch.delenv("FAKE_OPENCODE_DEBUG_CONFIG_FILE", raising=False)

    with pytest.raises(ProtocolError) as exc_info:
        recheck_control_plane(
            SubprocessRunner(RealClock()),
            executable=HELPER,
            workspace=_workspace(tmp_path),
            utility_timeout_seconds=UTILITY_TIMEOUT_SECONDS,
            termination_grace_seconds=TERMINATION_GRACE_SECONDS,
            expected_digest=evidence.control_plane_digest,
        )
    assert exc_info.value.code == "opencode.control_plane_drift"
    assert exc_info.value.causes
    assert exc_info.value.causes[0].code == "opencode.debug_config_invalid"

"""Component tests for the OpenCode `1.17.18` preflight (M07-02) and
sanitized-export identity verification (M07-05).

Spawns `tests/component/helpers/fake_opencode.py` through the real
`SubprocessRunner`, never a fixture or mock of `ProcessRunner` itself, to
prove `run_preflight`/`recheck_control_plane`/`run_export_and_verify_identity`
build the real argv sequences ADR-005 SS10.4/SS10.5 fix and fail closed on
every scenario System Design SS20.4 requires evidence for: call count, an
unknown version, a missing capability, an agent that is
missing/subagent/fallback, a permission-policy mismatch, control-plane
drift, and -- for export identity -- a wrong/missing/fallback agent, a
call timeout or non-zero exit, and output overflow.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools.domain import AgentRole, Workspace
from opencode_tools.errors import PreflightError, ProtocolError
from opencode_tools.opencode import (
    AgentIdentityEvidence,
    ControlPlaneEvidence,
    recheck_control_plane,
    run_export_and_verify_identity,
    run_preflight,
)
from opencode_tools.process import SubprocessRunner

HELPER = Path(__file__).resolve().parent / "helpers" / "fake_opencode.py"
FIXTURES_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "opencode" / "1.17.18"
)
DEBUG_FIXTURES = FIXTURES_ROOT / "debug"
EXPORT_FIXTURES = FIXTURES_ROOT / "export"

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


# --- AC-007: version/capability proof and effective-agent identity -----------


def test_ac_007_version_capability_and_effective_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (1) Positive: version and capability are proven, and all three roles'
    # effective agents are folded into one recorded control-plane digest.
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_OPENCODE_CALL_LOG_FILE", str(call_log))
    _set_baseline_debug_fixtures(monkeypatch)

    evidence = _preflight(tmp_path)

    assert evidence.version == "1.17.18"
    assert evidence.control_plane_digest
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "--version",
        "run --help",
        "debug config",
        "debug agent architect",
        "debug agent coder",
        "debug agent reviewer",
    ]

    # (2) Fail closed when a role's effective agent is missing outright.
    _set_baseline_debug_fixtures(monkeypatch)
    monkeypatch.setenv(
        "FAKE_OPENCODE_DEBUG_AGENT_ARCHITECT_FILE",
        str(DEBUG_FIXTURES / "agent-missing.json"),
    )
    with pytest.raises(PreflightError) as missing_agent_error:
        _preflight(tmp_path)
    assert missing_agent_error.value.code == "opencode.debug_agent_identity_mismatch"

    # (3) Fail closed on a silent fallback to another agent.
    fallback_file = tmp_path / "agent-fallback.json"
    fallback_file.write_text(
        '{"name": "general", "mode": "primary", '
        '"tools": {"ask": false, "task": false}, '
        '"permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"}}',
        encoding="utf-8",
    )
    _set_baseline_debug_fixtures(monkeypatch)
    monkeypatch.setenv("FAKE_OPENCODE_DEBUG_AGENT_ARCHITECT_FILE", str(fallback_file))
    with pytest.raises(PreflightError) as fallback_agent_error:
        _preflight(tmp_path)
    assert fallback_agent_error.value.code == "opencode.debug_agent_identity_mismatch"

    # (4) Positive at the M07-05 sanitized-export identity layer: the
    # verified agent recorded for the executed role is proven, not assumed.
    monkeypatch.setenv(
        "FAKE_OPENCODE_EXPORT_FILE", str(EXPORT_FIXTURES / "coder-correct-agent.json")
    )
    export_evidence = _export_identity(
        tmp_path, AgentRole.CODER, session_id="ses_coder_completed"
    )
    assert export_evidence.verified_agent == "coder"

    # (5) Fail closed at the export layer too: a mismatched or missing
    # agent field must not be silently accepted.
    monkeypatch.setenv(
        "FAKE_OPENCODE_EXPORT_FILE", str(EXPORT_FIXTURES / "agent-mismatch.json")
    )
    with pytest.raises(ProtocolError) as export_mismatch_error:
        _export_identity(
            tmp_path, AgentRole.ARCHITECT, session_id="ses_architect_ready"
        )
    assert export_mismatch_error.value.code == "opencode.export_agent_mismatch"

    monkeypatch.setenv(
        "FAKE_OPENCODE_EXPORT_FILE", str(EXPORT_FIXTURES / "agent-field-missing.json")
    )
    with pytest.raises(ProtocolError) as export_missing_error:
        _export_identity(tmp_path, AgentRole.CODER, session_id="ses_coder_completed")
    assert export_missing_error.value.code == "opencode.export_agent_missing"


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


# =============================================================================
# M07-05: effective agent identity via sanitized export
# =============================================================================


def _export_identity(
    tmp_path: Path,
    role: AgentRole,
    *,
    session_id: str = "ses_test",
    utility_timeout_seconds: float = UTILITY_TIMEOUT_SECONDS,
) -> AgentIdentityEvidence:
    return run_export_and_verify_identity(
        SubprocessRunner(RealClock()),
        executable=HELPER,
        workspace=_workspace(tmp_path),
        role=role,
        session_id=session_id,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=TERMINATION_GRACE_SECONDS,
    )


def test_run_export_and_verify_identity_calls_export_with_session_and_sanitize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_OPENCODE_CALL_LOG_FILE", str(call_log))
    monkeypatch.setenv(
        "FAKE_OPENCODE_EXPORT_FILE", str(EXPORT_FIXTURES / "coder-correct-agent.json")
    )

    evidence = _export_identity(
        tmp_path, AgentRole.CODER, session_id="ses_coder_completed"
    )

    assert evidence.verified_agent == "coder"
    assert evidence.digest
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "export ses_coder_completed --sanitize"
    ]


def test_run_export_and_verify_identity_fails_closed_on_agent_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FAKE_OPENCODE_EXPORT_FILE", str(EXPORT_FIXTURES / "agent-mismatch.json")
    )

    with pytest.raises(ProtocolError) as exc_info:
        _export_identity(
            tmp_path, AgentRole.ARCHITECT, session_id="ses_architect_ready"
        )
    assert exc_info.value.code == "opencode.export_agent_mismatch"


def test_run_export_and_verify_identity_fails_closed_when_agent_field_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FAKE_OPENCODE_EXPORT_FILE", str(EXPORT_FIXTURES / "agent-field-missing.json")
    )

    with pytest.raises(ProtocolError) as exc_info:
        _export_identity(tmp_path, AgentRole.CODER, session_id="ses_coder_completed")
    assert exc_info.value.code == "opencode.export_agent_missing"


def test_run_export_and_verify_identity_fails_closed_on_a_fallback_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FAKE_OPENCODE_EXPORT_FILE",
        str(EXPORT_FIXTURES / "ambiguous-multiple-assistant-agents.json"),
    )

    with pytest.raises(ProtocolError) as exc_info:
        _export_identity(tmp_path, AgentRole.CODER, session_id="ses_multi_terminal")
    assert exc_info.value.code == "opencode.export_ambiguous_agent"


def test_run_export_and_verify_identity_fails_closed_on_a_call_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OPENCODE_SLEEP_SECONDS", "2")

    with pytest.raises(ProtocolError) as exc_info:
        _export_identity(
            tmp_path, AgentRole.CODER, session_id="ses_x", utility_timeout_seconds=0.2
        )
    assert exc_info.value.code == "opencode.export_call_failed"


def test_run_export_and_verify_identity_fails_closed_on_a_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OPENCODE_EXIT_CODE", "1")

    with pytest.raises(ProtocolError) as exc_info:
        _export_identity(tmp_path, AgentRole.CODER, session_id="ses_x")
    assert exc_info.value.code == "opencode.export_call_failed"


def test_run_export_and_verify_identity_fails_closed_on_output_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge_export = tmp_path / "huge-export.json"
    huge_export.write_bytes(b"{" + b" " * (2 * 1024 * 1024) + b"}")
    monkeypatch.setenv("FAKE_OPENCODE_EXPORT_FILE", str(huge_export))

    with pytest.raises(ProtocolError) as exc_info:
        _export_identity(tmp_path, AgentRole.CODER, session_id="ses_x")
    assert exc_info.value.code == "opencode.export_call_failed"


def test_run_export_and_verify_identity_fails_closed_on_an_empty_session_id(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        _export_identity(tmp_path, AgentRole.CODER, session_id="")
    assert exc_info.value.code == "opencode.export_missing_session_id"

"""Unit tests for spec 170 security workflow gating in dispatch and monitor."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.config import PersonasConfig
from coordinare.graph.nodes.dispatch_performer import _role_runs_workflow


def _make_config_with_security_workflow() -> Any:
    """Build a config where security role has workflow='security'."""
    class _FakeConfig:
        pass

    cfg = _FakeConfig()

    # Mock role config with workflow
    class _FakeRoleConfig:
        workflow = "security"

    security_role = _FakeRoleConfig()

    # Mock performers with resolved_role
    class _FakePerformers:
        def resolved_role(self, role: str) -> Any:
            if role == "security":
                return security_role
            return None

    cfg.performers = _FakePerformers()
    cfg.personas = PersonasConfig()
    return cfg


def _make_config_without_workflow() -> Any:
    """Build a config where security role has no workflow."""
    class _FakeConfig:
        pass

    cfg = _FakeConfig()

    # Mock role config without workflow
    class _FakeRoleConfig:
        pass

    security_role = _FakeRoleConfig()

    # Mock performers with resolved_role
    class _FakePerformers:
        def resolved_role(self, role: str) -> Any:
            if role == "security":
                return security_role
            return None

    cfg.performers = _FakePerformers()
    cfg.personas = PersonasConfig()
    return cfg


# ---------------------------------------------------------------------------
# Tests for _role_runs_workflow helper
# ---------------------------------------------------------------------------

def test_role_runs_workflow_true_when_workflow_matches() -> None:
    """_role_runs_workflow returns True when role config has matching workflow."""
    config = _make_config_with_security_workflow()
    state = {"config": config}
    assert _role_runs_workflow(state, "security", "security") is True


def test_role_runs_workflow_false_when_no_workflow() -> None:
    """_role_runs_workflow returns False when role has no workflow."""
    config = _make_config_without_workflow()
    state = {"config": config}
    assert _role_runs_workflow(state, "security", "security") is False


def test_role_runs_workflow_false_when_workflow_mismatch() -> None:
    """_role_runs_workflow returns False when workflow name doesn't match."""
    config = _make_config_with_security_workflow()
    state = {"config": config}
    assert _role_runs_workflow(state, "security", "other") is False


def test_role_runs_workflow_false_when_role_none() -> None:
    """_role_runs_workflow returns False when role is None."""
    config = _make_config_with_security_workflow()
    state = {"config": config}
    assert _role_runs_workflow(state, None, "security") is False


def test_role_runs_workflow_false_when_config_none() -> None:
    """_role_runs_workflow returns False when config is None."""
    state = {"config": None}
    assert _role_runs_workflow(state, "security", "security") is False


# ---------------------------------------------------------------------------
# Tests for dispatch logic (without full dispatch_performer run)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_logic_skips_floor_when_workflow_enabled() -> None:
    """Simulating dispatch logic: when workflow is set, floor is skipped."""
    from coordinare.graph.nodes.dispatch_performer import _role_runs_workflow

    config = _make_config_with_security_workflow()
    state = {"config": config}

    # The dispatch floor would be skipped if this is True
    should_skip_floor = _role_runs_workflow(state, "security", "security")

    assert should_skip_floor is True


@pytest.mark.asyncio
async def test_dispatch_logic_runs_floor_when_workflow_disabled() -> None:
    """Simulating dispatch logic: when no workflow, floor is run."""
    from coordinare.graph.nodes.dispatch_performer import _role_runs_workflow

    config = _make_config_without_workflow()
    state = {"config": config}

    # The dispatch floor would be run if this is False
    should_skip_floor = _role_runs_workflow(state, "security", "security")

    assert should_skip_floor is False


# ---------------------------------------------------------------------------
# Tests for reset_review_findings_for_reviewer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_clears_on_reviewing_stage() -> None:
    """reset_review_findings_for_reviewer clears review_findings on reviewing dispatch."""
    from coordinare.graph.nodes.dispatch_performer import reset_review_findings_for_reviewer

    state = {"review_findings": {"some": "findings"}, "card_id": "ITEM_1"}
    result = reset_review_findings_for_reviewer(state, "reviewing")

    assert result is True
    assert state["review_findings"] is None


@pytest.mark.asyncio
async def test_reset_clears_on_security_stage() -> None:
    """reset_review_findings_for_reviewer also clears on security dispatch (spec 170)."""
    from coordinare.graph.nodes.dispatch_performer import reset_review_findings_for_reviewer

    state = {"review_findings": {"some": "findings"}, "card_id": "ITEM_1"}
    result = reset_review_findings_for_reviewer(state, "security")

    assert result is True
    assert state["review_findings"] is None


@pytest.mark.asyncio
async def test_reset_does_not_clear_on_implementing_stage() -> None:
    """reset_review_findings_for_reviewer does not clear on implementing dispatch."""
    from coordinare.graph.nodes.dispatch_performer import reset_review_findings_for_reviewer

    state = {"review_findings": {"some": "findings"}, "card_id": "ITEM_1"}
    result = reset_review_findings_for_reviewer(state, "implementing")

    assert result is False
    assert state["review_findings"] == {"some": "findings"}


# ---------------------------------------------------------------------------
# Tests for monitor helper functions
# ---------------------------------------------------------------------------

def test_lift_security_findings_with_implementer_routed_findings() -> None:
    """_lift_security_findings lifts implementer-routed findings into review_findings."""
    from coordinare.graph.nodes.monitor_performer import _lift_security_findings

    report = {
        "security": {
            "changed_files": [{"path": "test.py"}],
            "diff_truncated": False,
            "verdict": "security_failed",
            "covered_files": ["test.py"],
            "blocking": [
                {
                    "path": "test.py",
                    "line": 10,
                    "category": "injection",
                    "problem": "SQL injection",
                    "why_blocking": "User input concatenated into SQL",
                    "evidence": "query = query + request.args['id']",
                    "origin": "model",
                    "routing": "implementer",
                },
            ],
        },
    }

    state: dict[str, Any] = {
        "current_card": {"id": "ITEM_1"},
    }

    _lift_security_findings(state, report, "implementing")

    assert state["review_findings"] is not None
    assert state["review_findings"]["verdict"] == "changes_requested"
    assert len(state["review_findings"]["findings"]) == 1
    assert state["review_findings"]["findings"][0]["path"] == "test.py"
    assert state["review_findings"]["findings"][0]["line"] == 10
    assert state["review_findings"]["findings"][0]["category"] == "injection"


def test_lift_security_findings_does_not_lift_architect_routed() -> None:
    """_lift_security_findings does not lift architect-routed findings."""
    from coordinare.graph.nodes.monitor_performer import _lift_security_findings

    report = {
        "security": {
            "changed_files": [],
            "diff_truncated": False,
            "verdict": "security_failed",
            "covered_files": [],
            "blocking": [
                {
                    "path": "test.py",
                    "line": 10,
                    "category": "broken_authorization",
                    "problem": "Auth bypass",
                    "why_blocking": "User can access admin functions",
                    "evidence": "if user.role == 'admin':",
                    "origin": "model",
                    "routing": "architect",
                },
            ],
        },
    }

    state: dict[str, Any] = {
        "current_card": {"id": "ITEM_1"},
    }

    _lift_security_findings(state, report, "implementing")

    # No implementer findings, so review_findings should not be set
    assert "review_findings" not in state


def test_lift_security_findings_does_not_lift_to_architecting() -> None:
    """_lift_security_findings does not lift when target_stage is architecting."""
    from coordinare.graph.nodes.monitor_performer import _lift_security_findings

    report = {
        "security": {
            "changed_files": [],
            "diff_truncated": False,
            "verdict": "security_failed",
            "covered_files": [],
            "blocking": [
                {
                    "path": "test.py",
                    "line": 10,
                    "category": "injection",
                    "problem": "SQL injection",
                    "why_blocking": "SQL injection",
                    "evidence": "test",
                    "origin": "model",
                    "routing": "implementer",
                },
            ],
        },
    }

    state: dict[str, Any] = {
        "current_card": {"id": "ITEM_1"},
    }

    _lift_security_findings(state, report, "architecting")

    # Should not lift to architecting stage
    assert "review_findings" not in state


def test_lift_security_findings_handles_empty_report() -> None:
    """_lift_security_findings handles empty reports gracefully."""
    from coordinare.graph.nodes.monitor_performer import _lift_security_findings

    state: dict[str, Any] = {
        "current_card": {"id": "ITEM_1"},
    }

    _lift_security_findings(state, {}, "implementing")

    # Should not set review_findings for empty report
    assert "review_findings" not in state


def test_lift_security_findings_handles_no_security_key() -> None:
    """_lift_security_findings handles reports without security key."""
    from coordinare.graph.nodes.monitor_performer import _lift_security_findings

    report = {
        "workflow_metrics": {},
    }

    state: dict[str, Any] = {
        "current_card": {"id": "ITEM_1"},
    }

    _lift_security_findings(state, report, "implementing")

    # Should not set review_findings when security key is missing
    assert "review_findings" not in state


@pytest.mark.asyncio
async def test_a_security_env_blocked_hold_names_the_scanners_not_the_local_test_gate():
    """Review finding: the hold message for the security stage must not talk about local tests."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from tests.unit.graph.nodes.test_monitor_performer import _sec_state

    state = _sec_state(response={"status": "env_blocked", "reason": "semgrep: binary not found", "report": {"security": {"verdict": "env_blocked"}}}, scanner_findings=[])
    result = await monitor_performer(state)
    assert result["phase"] == "blocked"
    text = " ".join(result["open_questions"]) + result["system_error_reason"]
    assert "semgrep" in text and "security" in text.lower() and "local test" not in text.lower()

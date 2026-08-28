"""Tests for src/coordinare/dry_run.py (spec 038-dry-run-mode)."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from coordinare.dry_run import (
    DryRunAgentService,
    DryRunGitHubService,
    DryRunResult,
    execute_dry_run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_config(**overrides: Any) -> Any:
    """Build a minimal ProjectConfiguration-like object for dry-run tests.

    Uses a real ProjectConfiguration instance with env-var overrides so that
    pydantic validation passes.
    """
    env = {
        "COORDINARE_PROJECT_NAME": "test-project",
        "COORDINARE_GITHUB_ORG": "test-org",
        "COORDINARE_GITHUB_PROJECT_NUMBER": "1",
        "COORDINARE_HUMAN_REVIEWERS": '["alice"]',
        "COORDINARE_GITHUB_TOKEN": "ghp_test",
    }
    with patch.dict("os.environ", env, clear=True):
        from coordinare.config import ProjectConfiguration
        return ProjectConfiguration(**overrides)


# ---------------------------------------------------------------------------
# DryRunGitHubService
# ---------------------------------------------------------------------------

class TestDryRunGitHubService:
    """Verify every protocol method records the call and returns synthetic data."""

    def test_poll_board_records_action(self) -> None:
        svc = DryRunGitHubService()
        result = asyncio.run(svc.poll_board())
        assert len(svc.recorded_actions) == 1
        assert svc.recorded_actions[0]["method"] == "poll_board"
        assert "snapshot" in result

    def test_get_issue_details_records_action(self) -> None:
        svc = DryRunGitHubService()
        result = asyncio.run(svc.get_issue_details("ISSUE-42"))
        assert svc.recorded_actions[0]["method"] == "get_issue_details"
        assert svc.recorded_actions[0]["issue_id"] == "ISSUE-42"
        assert result["id"] == "ISSUE-42"

    def test_move_card_records_action(self) -> None:
        svc = DryRunGitHubService()
        asyncio.run(svc.move_card("ITEM-1", "IN_PROGRESS"))
        assert svc.recorded_actions[0]["method"] == "move_card"
        assert svc.recorded_actions[0]["status"] == "IN_PROGRESS"

    def test_get_pr_reviews_records_action(self) -> None:
        svc = DryRunGitHubService()
        result = asyncio.run(svc.get_pr_reviews("PR-1"))
        assert svc.recorded_actions[0]["method"] == "get_pr_reviews"
        assert result == []

    def test_check_mergeability_records_action(self) -> None:
        svc = DryRunGitHubService()
        result = asyncio.run(svc.check_mergeability("PR-1"))
        assert svc.recorded_actions[0]["method"] == "check_mergeability"
        assert result["mergeable"] is True

    def test_squash_merge_records_action(self) -> None:
        svc = DryRunGitHubService()
        result = asyncio.run(svc.squash_merge("PR-1"))
        assert svc.recorded_actions[0]["method"] == "squash_merge"
        assert result["merged"] is True

    def test_add_comment_records_action(self) -> None:
        svc = DryRunGitHubService()
        result = asyncio.run(svc.add_comment("SUBJ-1", "hello"))
        assert svc.recorded_actions[0]["method"] == "add_comment"
        assert svc.recorded_actions[0]["body"] == "hello"
        assert "id" in result

    def test_multiple_calls_accumulate(self) -> None:
        svc = DryRunGitHubService()
        asyncio.run(svc.poll_board())
        asyncio.run(svc.get_issue_details("X"))
        asyncio.run(svc.move_card("Y", "Done"))
        assert len(svc.recorded_actions) == 3


# ---------------------------------------------------------------------------
# DryRunAgentService
# ---------------------------------------------------------------------------

class TestDryRunAgentService:
    """Verify agent service mock returns synthetic responses."""

    def test_dispatch_card_returns_accepted(self) -> None:
        svc = DryRunAgentService()
        result = asyncio.run(
            svc.dispatch_card({"card_id": "C1"})
        )
        assert result["status"] == "accepted"
        assert result["session_id"] == "dry-run-session"
        assert svc.recorded_actions[0]["method"] == "dispatch_card"

    def test_check_status_returns_pr_opened(self) -> None:
        svc = DryRunAgentService()
        result = asyncio.run(
            svc.check_status("dry-run-session")
        )
        assert result["status"] == "pr_opened"
        assert "pr_url" in result
        assert "pr_node_id" in result

    def test_check_status_accepts_payload_kwarg(self) -> None:
        """042: monitor_performer now pushes a refreshed token through
        check_status via the ``payload`` kwarg.  DryRunAgentService must
        accept and record it, or dry-run mode crashes with
        ``TypeError: unexpected keyword argument 'payload'``."""
        svc = DryRunAgentService()
        payload = {"github_token": "refreshed-token-abc"}
        result = asyncio.run(
            svc.check_status("dry-run-session", payload=payload)
        )
        assert result["status"] == "pr_opened"
        # Payload must be captured in recorded_actions for auditability
        status_calls = [a for a in svc.recorded_actions if a.get("method") == "check_status"]
        assert status_calls
        assert status_calls[-1].get("payload") == payload

    def test_check_status_default_payload_is_none(self) -> None:
        """Backward compat: existing callers that don't pass payload still work."""
        svc = DryRunAgentService()
        asyncio.run(svc.check_status("sid"))
        status_calls = [a for a in svc.recorded_actions if a.get("method") == "check_status"]
        assert status_calls[-1].get("payload") is None

    def test_check_health_returns_healthy(self) -> None:
        svc = DryRunAgentService()
        result = asyncio.run(svc.check_health())
        assert result["status"] == "healthy"

    def test_relay_feedback_records_action(self) -> None:
        svc = DryRunAgentService()
        result = asyncio.run(
            svc.relay_feedback({"comment": "fix this"})
        )
        assert result["status"] == "accepted"
        assert svc.recorded_actions[0]["method"] == "relay_feedback"


# ---------------------------------------------------------------------------
# DryRunResult model
# ---------------------------------------------------------------------------

class TestDryRunResult:
    """Verify DryRunResult pydantic model."""

    def test_valid_construction(self) -> None:
        r = DryRunResult(
            planned_actions=["a", "b"],
            lifecycle_stages=["implementing"],
            assessment_result="sufficient",
            board_transitions=[{"stage": "implementing", "column": "IN_PROGRESS"}],
        )
        assert r.planned_actions == ["a", "b"]
        assert r.lifecycle_stages == ["implementing"]

    def test_model_dump(self) -> None:
        r = DryRunResult(
            planned_actions=["x"],
            lifecycle_stages=["reviewing"],
            assessment_result="sufficient",
            board_transitions=[],
        )
        d = r.model_dump()
        assert isinstance(d, dict)
        assert d["assessment_result"] == "sufficient"


# ---------------------------------------------------------------------------
# execute_dry_run
# ---------------------------------------------------------------------------

class TestExecuteDryRun:
    """Verify the orchestrator produces a valid DryRunResult."""

    def test_default_config_produces_implementing_stage(self) -> None:
        config = _make_minimal_config()
        result = asyncio.run(
            execute_dry_run("CARD-1", config)
        )
        assert isinstance(result, DryRunResult)
        assert "implementing" in result.lifecycle_stages
        assert result.assessment_result == "sufficient"
        assert len(result.planned_actions) > 0
        assert len(result.board_transitions) > 0
        # Must include card ID in actions
        assert any("CARD-1" in a for a in result.planned_actions)

    def test_multi_role_config_produces_multiple_stages(self) -> None:
        from coordinare.config import PerformerRoleConfig, PerformersConfig
        performers = PerformersConfig(
            implementer=PerformerRoleConfig(backend="opencode"),
            reviewer=PerformerRoleConfig(backend="opencode"),
        )
        config = _make_minimal_config(performers=performers)
        result = asyncio.run(
            execute_dry_run("CARD-2", config)
        )
        assert "implementing" in result.lifecycle_stages
        assert "reviewing" in result.lifecycle_stages
        assert len(result.lifecycle_stages) == 2

    def test_board_transitions_end_with_done(self) -> None:
        config = _make_minimal_config()
        result = asyncio.run(
            execute_dry_run("CARD-3", config)
        )
        last = result.board_transitions[-1]
        assert last["stage"] == "complete"
        assert last["column"] == "DONE"


# ---------------------------------------------------------------------------
# Dashboard API endpoint
# ---------------------------------------------------------------------------

class TestDryRunEndpoint:
    """Verify POST /api/dry-run/{card_id} returns correct JSON."""

    def _make_app(self, config: Any = None) -> TestClient:
        from coordinare.dashboard import DashboardStore, create_dashboard_app

        store = DashboardStore()
        daemon = MagicMock()
        daemon.state = {"phase": "idle", "error_count": 0}
        if config is not None:
            daemon.state["config"] = config
        daemon.state_store = MagicMock()
        daemon.state_store.last_snapshot = None
        daemon._cycle_active = False
        daemon.running = True

        metrics = MagicMock()
        metrics.cycles_completed_total._value.get.return_value = 0
        metrics.build_info.labels.return_value._value.get.return_value = {
            "started_at": "2026-03-28T00:00:00+00:00"
        }

        health = MagicMock()
        health.snapshot.return_value = MagicMock(
            probes=[], overall_status=MagicMock(value="healthy")
        )

        app = create_dashboard_app(store, daemon, metrics, health)
        return TestClient(app, base_url="http://127.0.0.1:8090")

    def test_dry_run_endpoint_returns_result(self) -> None:
        config = _make_minimal_config()
        client = self._make_app(config=config)
        resp = client.post("/api/dry-run/CARD-99")
        assert resp.status_code == 200
        body = resp.json()
        assert "planned_actions" in body
        assert "lifecycle_stages" in body
        assert "assessment_result" in body
        assert "board_transitions" in body
        assert body["assessment_result"] == "sufficient"

    def test_dry_run_endpoint_no_config_returns_500(self) -> None:
        client = self._make_app(config=None)
        resp = client.post("/api/dry-run/CARD-1")
        assert resp.status_code == 500
        assert "error" in resp.json()

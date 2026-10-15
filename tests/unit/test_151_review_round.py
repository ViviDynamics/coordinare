"""#151 review round: the blocks and follow-ups the independent review found.

Each test pins one defect: a bad signature alone must not degrade the webhook row, a
symphony name must never reach the shared disk/env row, a rejected token must read as
down, a missing docker binary must be recorded, and so on.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coordinare.dashboard import DashboardStore
from coordinare.dashboard.assets import _DASHBOARD_JS_DIR
from coordinare.dashboard.webhook import register_webhook_route
from coordinare.resilience import CircuitOpenError
from coordinare.services import http_performer_service as hps_mod
from coordinare.services.github import AuthGitHubError, GitHubService, PermanentGitHubError
from coordinare.services.system_health import (
    DEGRADED,
    DOWN,
    HEALTHY,
    MODEL_MIN_SAMPLE_TURNS,
    PERFORMER_START_FRESH_SECONDS,
    SYSTEM_HEALTH,
    WEBHOOK_REJECTION_RECORD_MAX,
    WEBHOOK_REJECTIONS_DEGRADED,
    SystemHealthInputs,
    build_system_health,
)
from tests.unit.services.test_http_performer_service import _card, _ephemeral_config, _workspace

_NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
_SCOPE_CHECKS = Path(__file__).resolve().parents[1] / "js" / "system_health_scope_checks.js"


@pytest.fixture(autouse=True)
def _fresh_recorder() -> None:
    SYSTEM_HEALTH.reset()


def _inputs(**overrides: Any) -> SystemHealthInputs:
    base: dict[str, Any] = {
        "now": _NOW,
        "health": {},
        "poll_interval_seconds": 30,
        "webhooks_enabled": True,
        "performer_modes": ("ephemeral",),
        "active_performers": 0,
        "disk_free_fraction": 0.5,
        "env_symphonies": 0,
        "env_failed": (),
        "env_ready": 0,
    }
    base.update(overrides)
    return SystemHealthInputs(**base)


def _row(key: str, **overrides: Any) -> dict[str, Any]:
    result = build_system_health(SYSTEM_HEALTH, _inputs(**overrides))
    return next(row for row in result["rows"] if row["key"] == key)


# --- Block 1: webhook rejections are counted in a window ----------------------------


def test_one_bad_signature_alone_does_not_degrade_the_webhook_row() -> None:
    SYSTEM_HEALTH.record_webhook(accepted=False, at=_NOW - timedelta(seconds=5))
    row = _row("github_webhooks")
    assert row["status"] != DEGRADED
    assert "1 rejected" in row["summary"]


def test_a_flood_of_bad_signatures_with_no_valid_delivery_degrades_the_row() -> None:
    for offset in range(WEBHOOK_REJECTIONS_DEGRADED):
        SYSTEM_HEALTH.record_webhook(accepted=False, at=_NOW - timedelta(seconds=offset + 1))
    row = _row("github_webhooks")
    assert row["status"] == DEGRADED
    assert "signature" in row["reason"]


def test_one_short_of_the_named_count_does_not_degrade() -> None:
    for offset in range(WEBHOOK_REJECTIONS_DEGRADED - 1):
        SYSTEM_HEALTH.record_webhook(accepted=False, at=_NOW - timedelta(seconds=offset + 1))
    assert _row("github_webhooks")["status"] != DEGRADED


def test_a_valid_delivery_after_the_rejections_clears_the_degradation() -> None:
    for offset in range(WEBHOOK_REJECTIONS_DEGRADED):
        SYSTEM_HEALTH.record_webhook(accepted=False, at=_NOW - timedelta(seconds=offset + 10))
    SYSTEM_HEALTH.record_webhook(accepted=True, at=_NOW - timedelta(seconds=1))
    assert _row("github_webhooks")["status"] == HEALTHY


def test_rejections_outside_the_window_do_not_count() -> None:
    for offset in range(WEBHOOK_REJECTIONS_DEGRADED):
        SYSTEM_HEALTH.record_webhook(accepted=False, at=_NOW - timedelta(days=2, seconds=offset))
    assert _row("github_webhooks")["status"] != DEGRADED


def test_the_rejection_record_is_bounded() -> None:
    for offset in range(WEBHOOK_REJECTION_RECORD_MAX * 3):
        SYSTEM_HEALTH.record_webhook(accepted=False, at=_NOW - timedelta(seconds=offset))
    assert len(SYSTEM_HEALTH.webhook_rejections) <= WEBHOOK_REJECTION_RECORD_MAX


def test_the_real_route_records_one_bad_signature_without_degrading() -> None:
    app = FastAPI()
    register_webhook_route(app, "/webhook/github", "s3cret", asyncio.Event())
    response = TestClient(app).post("/webhook/github", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bogus"})
    assert response.status_code == 401
    assert _row("github_webhooks")["status"] != DEGRADED


# --- Block 2: the shared disk/env row names no symphony -----------------------------


def test_the_shared_env_row_counts_failing_symphonies_and_names_none() -> None:
    row = _row("disk_env_cache", env_symphonies=2, env_failed=("alpha",), env_ready=1)
    assert row["status"] == DEGRADED
    assert "alpha" not in json.dumps(row)
    assert "env cache bootstrap failed for 1 symphony" in row["reason"]


def test_the_count_reads_plural_for_several_failing_symphonies() -> None:
    row = _row("disk_env_cache", env_symphonies=3, env_failed=("alpha", "beta"), env_ready=1)
    assert "env cache bootstrap failed for 2 symphonies" in row["reason"]
    assert "alpha" not in json.dumps(row) and "beta" not in json.dumps(row)


def test_the_scoped_view_of_another_symphony_carries_no_failing_symphony_name(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_health, _make_mock_metrics

    config = MagicMock()
    config.poll_interval_seconds = 30
    config.state_file_path = tmp_path / "coordinare.state.json"
    config.webhooks = SimpleNamespace(enabled=False)
    config.performer_endpoints = []
    daemon = _make_mock_daemon()
    daemon.state["config"] = config
    daemon.state["env_cache"] = {
        "alpha": SimpleNamespace(bootstrap_exhausted=True, last_bootstrap_succeeded=False),
        "beta": SimpleNamespace(bootstrap_exhausted=False, last_bootstrap_succeeded=True),
    }
    snapshot = DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health([]))
    snapshot["symphonies"] = [{"name": "alpha", "cycle_count": 1, "state": {}}, {"name": "beta", "cycle_count": 1, "state": {}}]
    snapshot["active_sessions"] = []
    snapshot["open_questions"] = []
    snapshot["token_usage"] = {}
    script = tmp_path / "snapshot.json"
    script.write_text(json.dumps(snapshot, default=str), encoding="utf-8")

    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    result = subprocess.run(
        [node, str(_SCOPE_CHECKS), str(_DASHBOARD_JS_DIR / "symphonies.js"), str(script)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, report
    assert "SUMMARY 4 0" in result.stdout, report


# --- Block 3: GitHub failures of every class are recorded -------------------------------


@pytest.mark.asyncio
async def test_a_rejected_token_reads_down_at_once_and_names_no_message(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = GitHubService(org="acme", project_number=1, token="t")
    monkeypatch.setattr(svc, "_retried_execute", AsyncMock(side_effect=AuthGitHubError("401 Bearer ghp_secret0123")))
    with pytest.raises(AuthGitHubError):
        await svc._guarded_execute("query { viewer { login } }", {})
    row = _row("github_api")
    assert row["status"] == DOWN
    assert row["reason"] == "GitHub rejected the token"
    assert "ghp_secret" not in json.dumps(row)


@pytest.mark.asyncio
async def test_an_open_circuit_reads_down_with_the_circuit_open_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = GitHubService(org="acme", project_number=1, token="t")
    monkeypatch.setattr(svc, "_retried_execute", AsyncMock(side_effect=CircuitOpenError("github open")))
    with pytest.raises(CircuitOpenError):
        await svc._guarded_execute("query { viewer { login } }", {})
    row = _row("github_api")
    assert row["status"] == DOWN
    assert row["reason"] == "circuit open"


@pytest.mark.asyncio
async def test_a_permanent_error_is_recorded_by_class_but_does_not_degrade_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = GitHubService(org="acme", project_number=1, token="t")
    monkeypatch.setattr(svc, "_retried_execute", AsyncMock(side_effect=PermanentGitHubError("UNPROCESSABLE")))
    with pytest.raises(PermanentGitHubError):
        await svc._guarded_execute("query { viewer { login } }", {})
    assert SYSTEM_HEALTH.github.last_error == "PermanentGitHubError"
    assert _row("github_api")["status"] != DEGRADED


# --- Follow-up: a missing docker binary is recorded, and still raised -----------------


@pytest.mark.asyncio
async def test_a_missing_docker_binary_is_recorded_and_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _missing(config: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", _missing)
    with pytest.raises(FileNotFoundError):
        await hps_mod.HTTPPerformerService(_ephemeral_config()).dispatch_card(_card(), _workspace())
    row = _row("performer_runtime")
    assert row["status"] != HEALTHY
    assert "FileNotFoundError" in row["reason"]


# --- Follow-up: webhook-only mode does not age the board row ----------------------------


def test_with_polling_off_a_quiet_repo_does_not_make_the_board_row_stale() -> None:
    SYSTEM_HEALTH.record_board_poll("acme/1", ok=True, at=_NOW - timedelta(hours=3))
    row = _row("board_check", poll_interval_seconds=None)
    assert row["status"] == HEALTHY
    assert row["last_success_at"] is not None
    assert "webhook" in row["summary"]


def test_with_polling_on_a_stale_board_still_degrades() -> None:
    SYSTEM_HEALTH.record_board_poll("acme/1", ok=True, at=_NOW - timedelta(hours=3))
    assert _row("board_check", poll_interval_seconds=30)["status"] == DEGRADED


# --- Follow-up: a rate reading whose reset has passed is stale, not nearly used up ------


def test_zero_remaining_after_its_reset_is_stale_not_nearly_used_up() -> None:
    SYSTEM_HEALTH.record_github_rate(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000",
         "X-RateLimit-Reset": str(int((_NOW - timedelta(minutes=5)).timestamp()))},
        at=_NOW - timedelta(minutes=5),
    )
    row = _row("github_api")
    assert row["status"] == "stale"
    assert "nearly used up" not in (row["reason"] or "")
    assert "reset" in row["reason"]


def test_zero_remaining_before_its_reset_is_still_down() -> None:
    SYSTEM_HEALTH.record_github_rate(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000",
         "X-RateLimit-Reset": str(int((_NOW + timedelta(minutes=5)).timestamp()))},
        at=_NOW,
    )
    assert _row("github_api")["status"] == DOWN


# --- Follow-up: a single old successful start is not green forever --------------------


def test_a_start_older_than_the_freshness_threshold_reads_stale_with_nothing_running() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW - timedelta(seconds=PERFORMER_START_FRESH_SECONDS + 60))
    row = _row("performer_runtime", active_performers=0)
    assert row["status"] == "stale"
    assert row["fresh_label"] == "last start"


def test_a_recent_start_stays_healthy() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW - timedelta(seconds=10))
    assert _row("performer_runtime", active_performers=0)["status"] == HEALTHY


def test_a_performer_in_flight_now_is_healthy_whatever_the_last_start_was() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW - timedelta(hours=5))
    assert _row("performer_runtime", active_performers=1)["status"] == HEALTHY


# --- Follow-up: the model rate needs a minimum sample -----------------------------------


def test_a_single_failed_turn_is_not_a_hundred_percent_degradation() -> None:
    SYSTEM_HEALTH.record_model_turn(ok=False, at=_NOW)
    row = _row("model_backends")
    assert row["status"] != DEGRADED
    assert row["summary"] == "1 of last 1 turns ended in a system error"


def test_two_consecutive_failures_degrade_below_the_sample_size() -> None:
    SYSTEM_HEALTH.record_model_turn(ok=True, at=_NOW)
    SYSTEM_HEALTH.record_model_turn(ok=False, at=_NOW)
    SYSTEM_HEALTH.record_model_turn(ok=False, at=_NOW)
    assert _row("model_backends")["status"] == DEGRADED


def test_the_rate_is_not_computed_below_the_minimum_sample() -> None:
    for ok in (False, True, False, True):
        SYSTEM_HEALTH.record_model_turn(ok=ok, at=_NOW)
    assert MODEL_MIN_SAMPLE_TURNS > 4
    assert _row("model_backends")["status"] != DEGRADED


def test_the_rate_applies_once_the_sample_is_reached() -> None:
    for ok in (False, False, True, True, True):
        SYSTEM_HEALTH.record_model_turn(ok=ok, at=_NOW)
    assert _row("model_backends")["status"] == DEGRADED


def test_the_model_reason_notes_that_system_errors_include_container_failures() -> None:
    for _ in range(3):
        SYSTEM_HEALTH.record_model_turn(ok=False, at=_NOW)
    assert "container" in _row("model_backends")["reason"]


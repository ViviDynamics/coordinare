"""#151 final check: the performer row counts only performers that are running, and a hung one is degraded.

A card waiting on CI or review (monitoring_pr, relay_feedback) has no performer running, so it
must not count as "in flight". A running performer with no activity past its threshold is degraded.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from coordinare.dashboard import DashboardStore
from coordinare.services.system_health import (
    DEGRADED,
    HEALTHY,
    PERFORMER_NO_ACTIVITY_FALLBACK_SECONDS,
    PERFORMER_RUNNING_PHASES,
    STALE,
    SYSTEM_HEALTH,
    SystemHealthInputs,
    build_system_health,
)
from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_health, _make_mock_metrics

_NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_recorder() -> None:
    SYSTEM_HEALTH.reset()


def _inputs(**overrides: Any) -> SystemHealthInputs:
    base: dict[str, Any] = {
        "now": _NOW,
        "health": {},
        "poll_interval_seconds": 30,
        "webhooks_enabled": False,
        "performer_modes": ("ephemeral",),
        "active_performers": 0,
        "disk_free_fraction": 0.5,
        "env_symphonies": 0,
        "env_failed": (),
        "env_ready": 0,
        "running_since": None,
        "last_activity_at": None,
        "no_activity_limit_seconds": PERFORMER_NO_ACTIVITY_FALLBACK_SECONDS,
    }
    base.update(overrides)
    return SystemHealthInputs(**base)


def _performer_row(**overrides: Any) -> dict[str, Any]:
    result = build_system_health(SYSTEM_HEALTH, _inputs(**overrides))
    return next(row for row in result["rows"] if row["key"] == "performer_runtime")


def _ephemeral_daemon(phase: str) -> Any:
    daemon = _make_mock_daemon(phase=phase)
    config = MagicMock()
    config.poll_interval_seconds = 30
    config.webhooks = SimpleNamespace(enabled=False)
    config.performer_endpoints = [SimpleNamespace(mode="ephemeral")]
    daemon.state["config"] = config
    return daemon


def _snapshot_for(phase: str, *, dispatched: datetime | None = None, stage: str = "implementing") -> dict[str, Any]:
    daemon = _ephemeral_daemon(phase)
    daemon.state["performer_stage"] = stage
    if dispatched is not None:
        daemon.state["agent_dispatch_at"] = dispatched
        daemon.state["agent_dispatch"] = {"container_id": "c-1", "backend": "claude"}
    return DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health([]))


def _store_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    return next(row for row in snapshot["system_health"]["rows"] if row["key"] == "performer_runtime")


# --- which phases mean a performer is running ----------------------------------------


def test_the_running_phases_are_the_ones_where_a_performer_job_is_live() -> None:
    assert set(PERFORMER_RUNNING_PHASES) == {"dispatching", "monitoring_agent", "monitoring_performer"}


def test_a_card_waiting_on_ci_has_no_performer_in_flight() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW - timedelta(hours=2))
    row = _store_row(_snapshot_for("monitoring_pr", dispatched=_NOW - timedelta(hours=2)))
    assert "0 performers in flight" in row["summary"]
    assert row["status"] == STALE


def test_a_card_waiting_on_relay_feedback_has_no_performer_in_flight() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW - timedelta(hours=2))
    row = _store_row(_snapshot_for("relay_feedback", dispatched=_NOW - timedelta(hours=2)))
    assert "0 performers in flight" in row["summary"]


def test_a_card_with_a_performer_running_counts_one_in_flight() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW)
    row = _store_row(_snapshot_for("monitoring_performer", dispatched=datetime.now(UTC)))
    assert "1 performer in flight" in row["summary"]


def test_the_row_for_a_waiting_card_is_not_healthy_when_nothing_has_run_recently() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW - timedelta(hours=2))
    row = _performer_row(active_performers=0, last_activity_at=None, running_since=None)
    assert row["status"] == STALE


# --- a hung in-flight performer is degraded -------------------------------------------


def test_a_running_performer_with_no_activity_past_its_threshold_is_degraded() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW - timedelta(hours=3))
    row = _performer_row(
        active_performers=1,
        running_since=_NOW - timedelta(hours=3),
        last_activity_at=_NOW - timedelta(hours=3),
        no_activity_limit_seconds=3600,
    )
    assert row["status"] == DEGRADED
    assert "no activity" in row["reason"]
    assert "running for" in row["reason"]


def test_a_running_performer_with_recent_activity_stays_healthy() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW)
    row = _performer_row(
        active_performers=1,
        running_since=_NOW - timedelta(hours=3),
        last_activity_at=_NOW - timedelta(seconds=30),
        no_activity_limit_seconds=3600,
    )
    assert row["status"] == HEALTHY


def test_the_running_stage_role_timeout_is_the_threshold_when_configured() -> None:
    daemon = _ephemeral_daemon("monitoring_performer")
    daemon.state["performer_stage"] = "implementing"
    daemon.state["agent_dispatch_at"] = datetime.now(UTC) - timedelta(minutes=20)
    daemon.state["agent_dispatch"] = {"container_id": "c-1", "backend": "claude"}
    daemon.state["role_timeouts"] = {"implementing": 600}
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=datetime.now(UTC))
    row = _store_row(DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health([])))
    assert row["status"] == DEGRADED
    assert "no activity" in row["reason"]


def test_without_a_configured_timeout_the_fallback_threshold_applies() -> None:
    SYSTEM_HEALTH.record_performer_start("DockerRuntime", ok=True, at=_NOW)
    within = _performer_row(
        active_performers=1, running_since=_NOW - timedelta(seconds=PERFORMER_NO_ACTIVITY_FALLBACK_SECONDS - 60),
        last_activity_at=None,
    )
    assert within["status"] == HEALTHY
    beyond = _performer_row(
        active_performers=1, running_since=_NOW - timedelta(seconds=PERFORMER_NO_ACTIVITY_FALLBACK_SECONDS + 60),
        last_activity_at=None,
    )
    assert beyond["status"] == DEGRADED


# --- wording: old rejections outside the window --------------------------------------


def test_old_rejections_with_no_valid_delivery_say_so_plainly() -> None:
    for offset in range(6):
        SYSTEM_HEALTH.record_webhook(accepted=False, at=_NOW - timedelta(days=2, seconds=offset))
    result = build_system_health(SYSTEM_HEALTH, _inputs(webhooks_enabled=True))
    row = next(r for r in result["rows"] if r["key"] == "github_webhooks")
    assert "no valid delivery since this daemon started" in row["summary"]
    assert "no delivery since" not in row["summary"]

"""Contract tests for POST /api/force-poll — all 6 invariants (016 T007)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from coordinare.dashboard import DashboardStore, create_dashboard_app


def _make_daemon(*, cycle_active: bool = False) -> MagicMock:
    daemon = MagicMock()
    daemon._cycle_active = cycle_active
    daemon._webhook_trigger = asyncio.Event()
    daemon.running = True
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    return daemon


def _make_health() -> MagicMock:
    health = MagicMock()
    health.snapshot.return_value.probes = []
    return health


def _make_metrics() -> MagicMock:
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": "2026-03-15T10:00:00+00:00"
    }
    return metrics


def _make_client(daemon: MagicMock) -> TestClient:
    store = MagicMock(spec=DashboardStore)
    app = create_dashboard_app(store, daemon, _make_metrics(), _make_health())
    return TestClient(app, raise_server_exceptions=True)


# I-001: POST /api/force-poll → 202 when daemon._cycle_active is False
def test_i001_returns_202_when_idle() -> None:
    daemon = _make_daemon(cycle_active=False)
    client = _make_client(daemon)
    resp = client.post("/api/force-poll")
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"


# I-002: POST /api/force-poll → 409 when daemon._cycle_active is True
def test_i002_returns_409_when_cycle_active() -> None:
    daemon = _make_daemon(cycle_active=True)
    client = _make_client(daemon)
    resp = client.post("/api/force-poll")
    assert resp.status_code == 409
    assert resp.json()["status"] == "cycle_in_progress"


# I-003: A 202 response causes daemon._webhook_trigger to be set
def test_i003_202_sets_webhook_trigger() -> None:
    daemon = _make_daemon(cycle_active=False)
    client = _make_client(daemon)
    resp = client.post("/api/force-poll")
    assert resp.status_code == 202
    assert daemon._webhook_trigger.is_set()


# I-004: A 409 response does NOT alter the daemon's trigger state
def test_i004_409_does_not_set_trigger() -> None:
    daemon = _make_daemon(cycle_active=True)
    assert not daemon._webhook_trigger.is_set()
    client = _make_client(daemon)
    resp = client.post("/api/force-poll")
    assert resp.status_code == 409
    assert not daemon._webhook_trigger.is_set()


# I-005: Every SSE state_update event includes the cycle_active boolean
def test_i005_sse_snapshot_contains_cycle_active() -> None:
    daemon = _make_daemon(cycle_active=False)
    snapshot = DashboardStore().build_snapshot(daemon, _make_metrics(), _make_health())
    assert "cycle_active" in snapshot
    assert isinstance(snapshot["cycle_active"], bool)


# I-006: cycle_active in SSE payload matches daemon._cycle_active at emission time
def test_i006_sse_cycle_active_matches_daemon_state() -> None:
    for expected in (True, False):
        daemon = _make_daemon(cycle_active=expected)
        snapshot = DashboardStore().build_snapshot(daemon, _make_metrics(), _make_health())
        assert snapshot["cycle_active"] == expected, (
            f"Expected cycle_active={expected}, got {snapshot['cycle_active']}"
        )

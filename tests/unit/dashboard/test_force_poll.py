"""Unit tests for the force-poll endpoint and SSE snapshot field (016 T003,T005,T006,T012,T013)."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from coordinare.dashboard import _DASHBOARD_HTML, DashboardStore, create_dashboard_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_daemon(*, cycle_active: bool = False, running: bool = True) -> MagicMock:
    daemon = MagicMock()
    daemon._cycle_active = cycle_active
    daemon._webhook_trigger = asyncio.Event()
    daemon.running = running
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


# ---------------------------------------------------------------------------
# T003: SSE snapshot includes cycle_active and daemon_running
# ---------------------------------------------------------------------------


class TestSnapshotCycleActiveField:
    def test_cycle_active_false_when_daemon_idle(self) -> None:
        daemon = _make_daemon(cycle_active=False, running=True)
        snapshot = DashboardStore().build_snapshot(daemon, _make_metrics(), _make_health())
        assert "cycle_active" in snapshot
        assert snapshot["cycle_active"] is False

    def test_cycle_active_true_when_daemon_busy(self) -> None:
        daemon = _make_daemon(cycle_active=True, running=True)
        snapshot = DashboardStore().build_snapshot(daemon, _make_metrics(), _make_health())
        assert snapshot["cycle_active"] is True

    def test_daemon_running_field_present(self) -> None:
        daemon = _make_daemon(cycle_active=False, running=True)
        snapshot = DashboardStore().build_snapshot(daemon, _make_metrics(), _make_health())
        assert "daemon_running" in snapshot
        assert snapshot["daemon_running"] is True


# ---------------------------------------------------------------------------
# T005: POST /api/force-poll → 202 when idle; sets webhook_trigger; < 50 ms
# ---------------------------------------------------------------------------


class TestForcePollEndpoint202:
    def test_returns_202_when_idle(self) -> None:
        daemon = _make_daemon(cycle_active=False)
        client = _make_client(daemon)
        resp = client.post("/api/force-poll")
        assert resp.status_code == 202
        assert resp.json() == {"status": "accepted"}

    def test_sets_webhook_trigger_when_idle(self) -> None:
        daemon = _make_daemon(cycle_active=False)
        client = _make_client(daemon)
        client.post("/api/force-poll")
        assert daemon._webhook_trigger.is_set()

    def test_response_time_under_50ms(self) -> None:
        """SC-001: trigger endpoint must respond in < 50 ms."""
        daemon = _make_daemon(cycle_active=False)
        client = _make_client(daemon)
        t0 = time.perf_counter()
        client.post("/api/force-poll")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50, f"Response took {elapsed_ms:.1f} ms (budget: 50 ms)"


# ---------------------------------------------------------------------------
# T006: POST /api/force-poll → 409 when cycle active; does NOT set trigger
# ---------------------------------------------------------------------------


class TestForcePollEndpoint409:
    def test_returns_409_when_cycle_active(self) -> None:
        daemon = _make_daemon(cycle_active=True)
        client = _make_client(daemon)
        resp = client.post("/api/force-poll")
        assert resp.status_code == 409
        assert resp.json() == {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"}

    def test_does_not_set_trigger_when_cycle_active(self) -> None:
        daemon = _make_daemon(cycle_active=True)
        client = _make_client(daemon)
        client.post("/api/force-poll")
        assert not daemon._webhook_trigger.is_set()


# ---------------------------------------------------------------------------
# T012: Button HTML present with correct id and aria-label
# ---------------------------------------------------------------------------


class TestDashboardHtmlButton:
    def test_force_poll_button_present(self) -> None:
        assert 'id="force-poll-btn"' in _DASHBOARD_HTML

    def test_force_poll_button_has_aria_label(self) -> None:
        assert 'aria-label="Trigger immediate board poll"' in _DASHBOARD_HTML

    def test_force_poll_message_element_present(self) -> None:
        assert 'id="force-poll-msg"' in _DASHBOARD_HTML


# ---------------------------------------------------------------------------
# T013: forcePoll() JS function present in dashboard HTML
# ---------------------------------------------------------------------------


class TestDashboardHtmlJs:
    def test_force_poll_function_defined(self) -> None:
        assert "async function forcePoll()" in _DASHBOARD_HTML

    def test_force_poll_posts_to_correct_endpoint(self) -> None:
        assert "fetch('/api/force-poll'" in _DASHBOARD_HTML

    def test_force_poll_handles_409(self) -> None:
        assert "Cycle already running" in _DASHBOARD_HTML

    def test_force_poll_handles_network_error(self) -> None:
        assert "Could not reach server" in _DASHBOARD_HTML

    def test_sse_handler_disables_button_on_cycle_active(self) -> None:
        assert "s.cycle_active" in _DASHBOARD_HTML

    def test_sse_handler_references_daemon_running(self) -> None:
        assert "s.daemon_running" in _DASHBOARD_HTML

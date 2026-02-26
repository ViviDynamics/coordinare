from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path
from fastapi.testclient import TestClient

from coordinare.daemon import CoordinareDaemon
from coordinare.health import create_health_app
from coordinare.metrics import CoordinareMetrics
from coordinare.state_store import StateStore, WorkflowSnapshot


class _Graph:
    async def ainvoke(self, state):
        return state


def _make_snapshot(**overrides) -> WorkflowSnapshot:
    defaults = {
        "snapshot_at": datetime.now(UTC),
        "phase": "idle",
    }
    defaults.update(overrides)
    return WorkflowSnapshot(**defaults)


# --- T024: Contract test — phase and snapshot_at present in response ---


def test_health_response_contains_phase_and_snapshot_at() -> None:
    """GET /health response contains phase (string or null) and snapshot_at (ISO 8601 or null)."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    assert "phase" in data
    assert "snapshot_at" in data
    # When no state_store, both should be None
    assert data["phase"] is None
    assert data["snapshot_at"] is None


def test_health_response_phase_is_valid_workflow_phase(tmp_path: Path) -> None:
    """phase field must be a valid WorkflowPhase string or null."""
    valid_phases = {
        "idle", "dispatching", "monitoring_agent", "monitoring_pr",
        "merging", "relay_feedback", "blocked", "recovery", None,
    }
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    store.last_snapshot = _make_snapshot(phase="monitoring_agent")

    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    assert data["phase"] in valid_phases


def test_health_response_snapshot_at_is_iso8601(tmp_path: Path) -> None:
    """snapshot_at field must be a valid ISO 8601 string when present."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    now = datetime.now(UTC)
    store.last_snapshot = _make_snapshot(phase="idle", snapshot_at=now)

    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    assert data["snapshot_at"] is not None
    # Should be parseable as ISO 8601
    parsed = datetime.fromisoformat(data["snapshot_at"])
    assert parsed.year == now.year


# --- T025: Contract test — phase and snapshot_at reflect last saved snapshot ---


@pytest.mark.asyncio
async def test_health_reflects_last_snapshot_before_first_poll(tmp_path: Path) -> None:
    """phase and snapshot_at reflect the last saved snapshot immediately after daemon start."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_HEALTH",
        active_card_title="Health Card",
        active_card_column="In Progress",
    )
    await store.save(snapshot)

    # Create daemon with state_store — load will be called in start()
    # But for this test we just set last_snapshot directly (simulating post-load)
    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    client = TestClient(create_health_app(daemon))

    response = client.get("/health")
    data = response.json()

    assert data["phase"] == "monitoring_agent"
    assert data["snapshot_at"] is not None


def test_health_null_phase_when_no_snapshot() -> None:
    """With no snapshot, phase and snapshot_at are null."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon))

    response = client.get("/health")
    data = response.json()

    assert data["phase"] is None
    assert data["snapshot_at"] is None


def test_health_response_required_fields_present(tmp_path: Path) -> None:
    """Response contains all required fields from the health-response schema."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    store.last_snapshot = _make_snapshot(phase="idle")

    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    # Required fields per schema
    assert "status" in data
    assert "uptime_seconds" in data
    assert "services" in data
    assert "timestamp" in data
    # New v2 fields
    assert "phase" in data
    assert "snapshot_at" in data
    # Services structure
    services = data["services"]
    assert "github" in services
    assert "agent_transport" in services
    assert "smtp" in services
    assert "slack" in services


# --- Coverage: card_payload, github_status, ready, metrics ---


def test_health_response_includes_current_card_when_present() -> None:
    """current_card field populated when daemon has an active card."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    daemon.state["current_card"] = {
        "id": "ITEM_1",
        "issue_number": 42,
        "title": "Test Card",
        "status": "IN_PROGRESS",
    }
    daemon.state["last_poll_at"] = datetime.now(UTC)
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    assert data["current_card"] is not None
    assert data["current_card"]["id"] == "ITEM_1"
    assert data["current_card"]["title"] == "Test Card"
    assert data["services"]["github"]["status"] == "connected"


def test_health_github_degraded_when_poll_stale() -> None:
    """GitHub status is 'degraded' when last_poll_at is older than 60s."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    daemon.state["last_poll_at"] = datetime(2020, 1, 1, tzinfo=UTC)
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    assert data["services"]["github"]["status"] == "degraded"


def test_health_github_unknown_when_no_poll() -> None:
    """GitHub status is 'unknown' when daemon is running but no poll_at."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    daemon.state["last_poll_at"] = None
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    assert data["services"]["github"]["status"] == "unknown"


def test_ready_endpoint_returns_503_when_not_running() -> None:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon))
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_metrics_endpoint_returns_text() -> None:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon))
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_ready_endpoint_returns_200_when_running() -> None:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    client = TestClient(create_health_app(daemon))
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True

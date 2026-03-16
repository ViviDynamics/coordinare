"""Tests for /health and /metrics endpoints (covering health.py lines 33-64, 104)."""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from coordinare.health import create_health_app
from coordinare.observability import HealthRegistry
from coordinare.resilience import CircuitBreaker, CircuitState


def _make_daemon(running: bool = True, state_store=None) -> MagicMock:
    daemon = MagicMock()
    daemon.running = running
    daemon.state_store = state_store
    return daemon


def _make_client(
    running: bool = True,
    circuit_breakers: dict | None = None,
    state_store=None,
) -> TestClient:
    registry = HealthRegistry(timeout_seconds=5)
    daemon = _make_daemon(running=running, state_store=state_store)
    app = create_health_app(
        daemon,
        circuit_breakers=circuit_breakers or {},
        health_registry=registry,
    )
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# /health — daemon not running
# ---------------------------------------------------------------------------


def test_health_unhealthy_when_daemon_not_running() -> None:
    client = _make_client(running=False)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unhealthy"


def test_health_returns_phase_idle_when_no_state_store() -> None:
    client = _make_client(running=False)
    resp = client.get("/health")
    body = resp.json()
    assert body["phase"] == "idle"
    assert body["snapshot_at"] is None


# ---------------------------------------------------------------------------
# /health — daemon running, no circuits
# ---------------------------------------------------------------------------


def test_health_ok_when_running_no_circuits() -> None:
    client = _make_client(running=True)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_includes_uptime_seconds() -> None:
    client = _make_client(running=True)
    resp = client.get("/health")
    body = resp.json()
    assert "uptime_seconds" in body
    assert body["uptime_seconds"] >= 0


def test_health_includes_empty_circuit_breakers() -> None:
    client = _make_client(running=True)
    resp = client.get("/health")
    assert resp.json()["circuit_breakers"] == {}


# ---------------------------------------------------------------------------
# /health — circuit breaker states
# ---------------------------------------------------------------------------


def test_health_degraded_when_core_circuit_open() -> None:
    """An open 'github' circuit (core) makes status degraded."""
    cb = CircuitBreaker(
        service_name="github",
        failure_threshold=1,
        recovery_window=60,
        observation_window=120,
    )
    # Force open by recording a failure
    cb._failure_times.append(1e18)
    cb._transition(CircuitState.OPEN, "test")

    client = _make_client(running=True, circuit_breakers={"github": cb})
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["circuit_breakers"]["github"]["state"] == "open"


def test_health_ok_when_non_core_circuit_open() -> None:
    """A non-core circuit (e.g. 'slack') open does NOT degrade /health status."""
    cb = CircuitBreaker(
        service_name="slack",
        failure_threshold=1,
        recovery_window=60,
        observation_window=120,
    )
    cb._transition(CircuitState.OPEN, "test")

    client = _make_client(running=True, circuit_breakers={"slack": cb})
    resp = client.get("/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert body["circuit_breakers"]["slack"]["state"] == "open"


def test_health_circuit_breakers_shows_closed_state() -> None:
    cb = CircuitBreaker(
        service_name="anthropic",
        failure_threshold=3,
        recovery_window=60,
        observation_window=120,
    )
    client = _make_client(running=True, circuit_breakers={"anthropic": cb})
    resp = client.get("/health")
    entry = resp.json()["circuit_breakers"]["anthropic"]
    assert entry["state"] == "closed"
    assert entry["failure_count"] == 0
    assert entry["opened_at"] is None


# ---------------------------------------------------------------------------
# /health — with state_store snapshot
# ---------------------------------------------------------------------------


def test_health_shows_phase_from_state_store_snapshot() -> None:
    from datetime import UTC, datetime

    from coordinare.state_store import WorkflowSnapshot

    snapshot = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="monitoring_agent",
        active_card_id="card-1",
    )
    state_store = MagicMock()
    state_store.last_snapshot = snapshot

    client = _make_client(running=True, state_store=state_store)
    resp = client.get("/health")
    body = resp.json()
    assert body["phase"] == "monitoring_agent"
    assert body["snapshot_at"] is not None


def test_health_idle_when_state_store_last_snapshot_is_none() -> None:
    state_store = MagicMock()
    state_store.last_snapshot = None

    client = _make_client(running=True, state_store=state_store)
    resp = client.get("/health")
    body = resp.json()
    assert body["phase"] == "idle"
    assert body["snapshot_at"] is None


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------


def test_metrics_returns_200_with_prometheus_format() -> None:
    client = _make_client(running=True)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# HealthRegistry.update() — unregistered probe → early return (line 116)
# ---------------------------------------------------------------------------


def test_health_registry_update_ignores_unregistered_probe() -> None:
    """Line 116 of observability.py: update() silently returns for unknown probe names."""
    from coordinare.observability import HealthStatus

    registry = HealthRegistry(timeout_seconds=5)
    # 'unknown-probe' is not registered → should not raise, state unchanged
    registry.update("unknown-probe", HealthStatus.healthy)
    snapshot = registry.snapshot()
    assert all(p.subsystem_name != "unknown-probe" for p in snapshot.probes)

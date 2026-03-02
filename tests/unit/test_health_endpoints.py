"""Tests for /live and /ready health endpoints (spec 009 US3, T019)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from coordinare.health import create_health_app
from coordinare.observability import HealthProbe, HealthRegistry, HealthStatus


def _make_daemon(running: bool = True) -> MagicMock:
    daemon = MagicMock()
    daemon.running = running
    daemon.state_store = None
    daemon.state = {}
    return daemon


def _make_app(registry: HealthRegistry, running: bool = True) -> TestClient:
    daemon = _make_daemon(running)
    app = create_health_app(daemon, health_registry=registry)
    return TestClient(app, raise_server_exceptions=True)


def _healthy_registry(*names: str, required: bool = True) -> HealthRegistry:
    reg = HealthRegistry(timeout_seconds=5)
    for name in names:
        reg.register(name, required=required)
        reg.update(name, HealthStatus.healthy)
    return reg


# ---------------------------------------------------------------------------
# /live
# ---------------------------------------------------------------------------


def test_live_always_200() -> None:
    reg = _healthy_registry("github")
    client = _make_app(reg)
    resp = client.get("/live")
    assert resp.status_code == 200


def test_live_returns_alive_body() -> None:
    reg = _healthy_registry("github")
    client = _make_app(reg)
    resp = client.get("/live")
    assert resp.json() == {"status": "alive"}


# ---------------------------------------------------------------------------
# /ready — success cases
# ---------------------------------------------------------------------------


def test_ready_200_when_all_required_healthy() -> None:
    reg = _healthy_registry("github", "agent_ssh")
    client = _make_app(reg)
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_ready_200_when_only_optional_subsystem_degraded() -> None:
    reg = HealthRegistry(timeout_seconds=5)
    reg.register("github", required=True)
    reg.update("github", HealthStatus.healthy)
    reg.register("notifications", required=False)
    reg.update("notifications", HealthStatus.degraded, details="Slack unreachable")

    client = _make_app(reg)
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


# ---------------------------------------------------------------------------
# /ready — failure cases
# ---------------------------------------------------------------------------


def test_ready_503_when_required_subsystem_unavailable() -> None:
    """register() initializes probes as unavailable; required unavailable → 503."""
    reg = HealthRegistry(timeout_seconds=5)
    reg.register("github", required=True)
    # Deliberately do NOT call reg.update() — probe stays at initial unavailable state

    client = _make_app(reg)
    resp = client.get("/ready")
    assert resp.status_code == 503
    github_entry = next(s for s in resp.json()["subsystems"] if s["name"] == "github")
    assert github_entry["status"] == "unavailable"


def test_ready_503_when_required_subsystem_degraded() -> None:
    reg = HealthRegistry(timeout_seconds=5)
    reg.register("github", required=True)
    reg.update("github", HealthStatus.degraded, details="API timeout")

    client = _make_app(reg)
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


def test_ready_includes_all_registered_subsystems() -> None:
    reg = HealthRegistry(timeout_seconds=5)
    for name in ("github", "agent_ssh", "notifications"):
        reg.register(name, required=True)
        reg.update(name, HealthStatus.healthy)

    client = _make_app(reg)
    resp = client.get("/ready")
    names = {s["name"] for s in resp.json()["subsystems"]}
    assert names == {"github", "agent_ssh", "notifications"}


def test_ready_marks_stale_probe_as_degraded() -> None:
    """A probe older than timeout_seconds is reported as degraded."""
    reg = HealthRegistry(timeout_seconds=1)
    reg.register("github", required=True)
    # Manually backdate the checked_at timestamp
    reg._probes["github"] = HealthProbe(
        subsystem_name="github",
        status=HealthStatus.healthy,
        is_required=True,
        checked_at=datetime.now(UTC) - timedelta(seconds=10),
    )

    client = _make_app(reg)
    resp = client.get("/ready")
    assert resp.status_code == 503
    github_entry = next(s for s in resp.json()["subsystems"] if s["name"] == "github")
    assert github_entry["status"] == "degraded"


def test_ready_response_time_ms_is_present_and_positive() -> None:
    reg = _healthy_registry("github")
    client = _make_app(reg)
    resp = client.get("/ready")
    body = resp.json()
    assert "response_time_ms" in body
    assert body["response_time_ms"] >= 0


def test_ready_stale_unavailable_probe_stays_unavailable() -> None:
    """A stale unavailable probe stays unavailable — staleness only downgrades healthy probes."""
    reg = HealthRegistry(timeout_seconds=1)
    reg.register("github", required=True)
    # Backdate checked_at: probe was registered but never updated
    reg._probes["github"] = HealthProbe(
        subsystem_name="github",
        status=HealthStatus.unavailable,
        is_required=True,
        checked_at=datetime.now(UTC) - timedelta(seconds=10),
    )

    client = _make_app(reg)
    resp = client.get("/ready")
    assert resp.status_code == 503
    entry = next(s for s in resp.json()["subsystems"] if s["name"] == "github")
    assert entry["status"] == "unavailable"  # not "degraded"


def test_optional_subsystems_config_registers_subsystem_as_not_required() -> None:
    """Subsystem registered with required=False does not affect overall readiness (FR-009)."""
    reg = HealthRegistry(timeout_seconds=5)
    reg.register("github", required=True)
    reg.update("github", HealthStatus.healthy)
    reg.register("notifications", required=False)
    reg.update("notifications", HealthStatus.degraded)

    client = _make_app(reg)
    resp = client.get("/ready")
    assert resp.status_code == 200

    notif_entry = next(s for s in resp.json()["subsystems"] if s["name"] == "notifications")
    assert notif_entry["required"] is False
    assert notif_entry["status"] == "degraded"

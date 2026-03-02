from __future__ import annotations

from fastapi.testclient import TestClient

from coordinare.daemon import CoordinareDaemon
from coordinare.health import create_health_app
from coordinare.observability import HealthRegistry, HealthStatus


class _Graph:
    async def ainvoke(self, state):
        return state


def _make_degraded_registry() -> HealthRegistry:
    """Return a HealthRegistry with one required degraded subsystem."""
    registry = HealthRegistry()
    registry.register("github", required=True)
    registry.update("github", HealthStatus.degraded, details="test degraded")
    return registry


def test_health_contract_routes_present() -> None:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    app = create_health_app(daemon)
    paths = {route.path for route in app.routes}

    assert "/health" in paths
    assert "/ready" in paths
    assert "/metrics" in paths


def test_ready_contract_payload() -> None:
    """Degraded required subsystem → 503 with 'status' field in body."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    registry = _make_degraded_registry()
    client = TestClient(create_health_app(daemon, health_registry=registry))
    response = client.get("/ready")

    assert response.status_code == 503
    assert "status" in response.json()

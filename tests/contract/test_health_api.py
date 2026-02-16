from __future__ import annotations

from fastapi.testclient import TestClient

from coordinare.daemon import CoordinareDaemon
from coordinare.health import create_health_app


class _Graph:
    async def ainvoke(self, state):
        return state

def test_health_contract_routes_present() -> None:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    app = create_health_app(daemon)
    paths = {route.path for route in app.routes}

    assert "/health" in paths
    assert "/ready" in paths
    assert "/metrics" in paths


def test_ready_contract_payload() -> None:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon))
    response = client.get("/ready")

    assert response.status_code == 503
    assert "ready" in response.json()

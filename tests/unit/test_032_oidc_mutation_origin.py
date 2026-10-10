from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import DashboardOidcConfig
from coordinare.dashboard_auth import DashboardAuthentication
from coordinare.dashboard_oidc import OidcFlow


def authenticated_app() -> tuple[DashboardAuthentication, str]:
    flow = OidcFlow(DashboardOidcConfig(
        discovery_url="https://identity.example/.well-known/openid-configuration",
        client_id="synthetic-client", client_secret=SecretStr("synthetic-secret"),
        redirect_url="https://dashboard.example/oidc/callback",
    ))
    session = flow.sessions.create("operator@example.test", lifetime_seconds=60)
    app = FastAPI()

    @app.post("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    return DashboardAuthentication(app, oidc=flow), session


@pytest.mark.parametrize("origin,status", [
    ("https://dashboard.example", 200),
    ("https://dashboard.example:443", 200),
    ("https://DASHBOARD.EXAMPLE", 200),
    ("http://dashboard.example", 403),
    ("https://unrelated.example", 403),
    ("https://dashboard.example:8443", 403),
])
def test_authenticated_mutations_use_configured_public_origin_after_tls_termination(origin: str, status: int) -> None:
    app, session = authenticated_app()
    with TestClient(app, base_url="http://dashboard.example") as client:
        response = client.post("/probe", headers={
            "Cookie": f"coordinare_session={session}", "Origin": origin,
        })
    assert response.status_code == status


@pytest.mark.parametrize("cookie", ["", "unknown-session"])
def test_matching_public_origin_does_not_replace_authentication(cookie: str) -> None:
    app, _session = authenticated_app()
    with TestClient(app, base_url="http://dashboard.example") as client:
        response = client.post("/probe", headers={
            "Cookie": f"coordinare_session={cookie}", "Origin": "https://dashboard.example",
        })
    assert response.status_code == 401


def test_oidc_mutation_without_origin_retains_authenticated_api_behavior() -> None:
    app, session = authenticated_app()
    with TestClient(app, base_url="http://dashboard.example") as client:
        response = client.post("/probe", headers={"Cookie": f"coordinare_session={session}"})
    assert response.status_code == 200

"""User Story 2 (497): boundary behaviour — API/SSE stay JSON-401, guard applies."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import DashboardOidcConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard_oidc import OidcFlow
from coordinare.localhost_guard import build_permitted, is_loopback_bind
from tests.unit.dashboard.oidc_double import CLIENT_ID, CLIENT_SECRET, ISSUER, ProviderDouble

BASE = "http://127.0.0.1:8090"
TOKEN = "a-secret-operator-token-of-at-least-32-characters"


def _oidc_config() -> DashboardOidcConfig:
    return DashboardOidcConfig(
        discovery_url=f"{ISSUER}/.well-known/openid-configuration",
        client_id=CLIENT_ID,
        client_secret=SecretStr(CLIENT_SECRET),
        redirect_url="https://dashboard.example/oidc/callback",
    )


def _app(**overrides) -> object:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    overrides.setdefault("permitted_origins", build_permitted(
        dashboard_host="127.0.0.1", dashboard_port=8090,
    ))
    overrides.setdefault("oidc", OidcFlow(_oidc_config()))
    return create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), **overrides)


def test_api_endpoint_returns_401_not_redirect_when_unauthenticated():
    with TestClient(_app(), base_url=BASE) as client:
        response = client.get("/api/personas", follow_redirects=False)
        assert response.status_code == 401
        assert response.headers.get("location") is None


def test_sse_returns_401_not_redirect_when_unauthenticated():
    with TestClient(_app(), base_url=BASE) as client:
        response = client.get("/events", follow_redirects=False)
        assert response.status_code == 401
        assert response.headers.get("location") is None


def test_oidc_routes_skip_token_authentication():
    double = ProviderDouble()
    with TestClient(_app(oidc=OidcFlow(_oidc_config(), client=_client(double))), base_url=BASE) as client:
        response = client.get(
            "/oidc/login",
            headers={"Authorization": f"Bearer {TOKEN}wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 302, response.text


def _client(double: ProviderDouble):
    import httpx

    return httpx.AsyncClient(transport=double.transport())


def test_logout_refuses_foreign_origin():
    with TestClient(_app(), base_url=BASE) as client:
        response = client.post(
            "/oidc/logout",
            headers={"Origin": "http://evil.example"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert response.json()["error"] == "rejected_by_localhost_guard"


def test_logout_refuses_loopback_origin_that_is_not_the_public_origin():
    flow = OidcFlow(_oidc_config(), client=_client(ProviderDouble()))
    with TestClient(_app(oidc=flow), base_url=BASE) as client:
        response = client.post(
            "/oidc/logout",
            headers={"Origin": "http://127.0.0.1:8090"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Cross-origin logout refused"


def test_non_loopback_bind_valid_with_oidc_without_token():
    from coordinare.dashboard_auth import validate_dashboard_bind

    validate_dashboard_bind("0.0.0.0", None, oidc=OidcFlow(_oidc_config()))


def test_non_loopback_bind_refused_without_token_and_oidc():
    from coordinare.dashboard_auth import validate_dashboard_bind

    with pytest.raises(ValueError, match="requires dashboard_auth_token"):
        validate_dashboard_bind("0.0.0.0", None, oidc=None)


def test_short_token_still_refused_even_with_oidc():
    from coordinare.dashboard_auth import validate_dashboard_bind

    with pytest.raises(ValueError, match="at least 32"):
        validate_dashboard_bind("0.0.0.0", SecretStr("short"), oidc=OidcFlow(_oidc_config()))


def test_is_loopback_bind_unchanged():
    assert is_loopback_bind("127.0.0.1")
    assert not is_loopback_bind("0.0.0.0")

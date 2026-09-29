"""User Story 3 (497): bearer token and OIDC coexist; default posture unchanged."""
from __future__ import annotations

from unittest.mock import MagicMock
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import DashboardOidcConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard_oidc import OidcFlow
from coordinare.localhost_guard import build_permitted
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


def _app(*, auth_token: SecretStr | None = None, with_oidc: bool = True) -> object:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    kwargs: dict[str, object] = {"permitted_origins": build_permitted(
        dashboard_host="127.0.0.1", dashboard_port=8090,
    )}
    if auth_token is not None:
        kwargs["auth_token"] = auth_token
    if with_oidc:
        double = ProviderDouble()
        kwargs["oidc"] = OidcFlow(
            _oidc_config(), client=httpx.AsyncClient(transport=double.transport()),
        )
    return create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), **kwargs)


def test_valid_token_authenticates_without_oidc_round_trip():
    with TestClient(_app(auth_token=SecretStr(TOKEN)), base_url=BASE) as client:
        response = client.get(
            "/", headers={"Authorization": f"Bearer {TOKEN}"},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert response.headers.get("location") is None


def test_invalid_token_on_page_load_redirects_to_login():
    with TestClient(_app(auth_token=SecretStr(TOKEN)), base_url=BASE) as client:
        response = client.get(
            "/", headers={"Authorization": "Bearer wrong-token"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert urlparse(response.headers["location"]).path == "/oidc/login"


def test_invalid_token_on_api_returns_401():
    with TestClient(_app(auth_token=SecretStr(TOKEN)), base_url=BASE) as client:
        response = client.get(
            "/api/personas", headers={"Authorization": "Bearer wrong-token"},
            follow_redirects=False,
        )
        assert response.status_code == 401


def test_default_posture_unchanged_without_token_and_oidc():
    with TestClient(_app(with_oidc=False), base_url=BASE) as client:
        assert client.get("/").status_code == 200

"""User Story 4 (497): session lifetime, cookie flags, logout, restart amnesia."""
from __future__ import annotations

import time
from http.cookies import SimpleCookie
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import DashboardOidcConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard_oidc import OidcFlow, OidcSessionStore
from coordinare.localhost_guard import build_permitted
from tests.unit.dashboard.oidc_double import CLIENT_ID, CLIENT_SECRET, ISSUER, ProviderDouble

BASE = "https://dashboard.example"


def _oidc_config() -> DashboardOidcConfig:
    return DashboardOidcConfig(
        discovery_url=f"{ISSUER}/.well-known/openid-configuration",
        client_id=CLIENT_ID,
        client_secret=SecretStr(CLIENT_SECRET),
        redirect_url="https://dashboard.example/oidc/callback",
    )


def _app(double: ProviderDouble | None = None) -> object:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    flow = OidcFlow(
        _oidc_config(),
        client=httpx.AsyncClient(
            transport=(double or ProviderDouble()).transport(),
        ),
    )
    return create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        permitted_origins=build_permitted(
            dashboard_host="dashboard.example", dashboard_port=443,
        ),
        oidc=flow,
    )


def _log_in(client: TestClient, double: ProviderDouble) -> None:
    client.get("/", follow_redirects=False)
    login = client.get("/oidc/login", follow_redirects=False)
    assert login.status_code == 302, login.text
    nonce = parse_qs(urlparse(login.headers["location"]).query)["nonce"][0]
    double.id_nonce = nonce
    code = double.issue_code()
    response = client.get(
        f"/oidc/callback?code={code}&state={client.cookies.get('oidc_state')}",
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    assert response.cookies.get("coordinare_session")


def _set_cookies(response) -> list[SimpleCookie]:
    parsed = []
    for raw in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(raw)
        parsed.append(cookie)
    return parsed


def _session_cookie(response) -> SimpleCookie:
    for cookie in _set_cookies(response):
        if "coordinare_session" in cookie:
            return cookie["coordinare_session"]
    raise AssertionError("coordinare_session cookie missing")


def test_session_cookie_flags():
    double = ProviderDouble()
    with TestClient(_app(double), base_url=BASE) as client:
        client.get("/", follow_redirects=False)
        login = client.get("/oidc/login", follow_redirects=False)
        nonce = parse_qs(urlparse(login.headers["location"]).query)["nonce"][0]
        double.id_nonce = nonce
        response = client.get(
            f"/oidc/callback?code={double.issue_code()}&state={client.cookies.get('oidc_state')}",
            follow_redirects=False,
        )
        assert response.status_code == 302, response.text
        session = _session_cookie(response)
        assert session["httponly"]
        assert session["samesite"].lower() == "lax"
        assert session["path"] == "/"
        assert session["secure"]


def test_state_cookies_are_short_lived():
    double = ProviderDouble()
    with TestClient(_app(double), base_url=BASE) as client:
        client.get("/", follow_redirects=False)
        login = client.get("/oidc/login", follow_redirects=False)
        assert login.status_code == 302
        for cookie in _set_cookies(login):
            names = list(cookie.keys())
            assert len(names) == 1
            if names[0] in ("oidc_state", "oidc_nonce"):
                assert cookie[names[0]]["httponly"]
                max_age = cookie[names[0]]["max-age"]
                assert max_age is not None and 0 < int(max_age) <= 300


def test_session_expires_after_configured_lifetime():
    now = {"value": time.time()}
    store = OidcSessionStore(clock=lambda: now["value"])
    value = store.create("operator@example.test", lifetime_seconds=3600)
    assert store.lookup(value) is not None
    now["value"] += 3601
    assert store.lookup(value) is None


def test_logout_revokes_session():
    double = ProviderDouble()
    with TestClient(_app(double), base_url=BASE) as client:
        _log_in(client, double)
        assert client.get("/", follow_redirects=False).status_code == 200
        logout = client.post(
            "/oidc/logout",
            headers={"Origin": "https://dashboard.example"},
            follow_redirects=False,
        )
        assert logout.status_code == 204
        assert client.get("/", follow_redirects=False).status_code == 302


def test_fresh_store_has_no_sessions_after_restart():
    store = OidcSessionStore()
    value = store.create("operator@example.test", lifetime_seconds=3600)
    assert store.lookup(value) is not None
    restarted = OidcSessionStore()
    assert restarted.lookup(value) is None

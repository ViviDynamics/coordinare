"""User Story 1 (497): the Authorization Code login round trip."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import DashboardOidcConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard_oidc import OidcFlow
from coordinare.localhost_guard import build_permitted
from tests.unit.dashboard.oidc_double import (
    CLIENT_ID,
    CLIENT_SECRET,
    ISSUER,
    ProviderDouble,
)

BASE = "https://127.0.0.1:8090"


def _oidc_config(**overrides) -> DashboardOidcConfig:
    fields: dict[str, object] = {
        "discovery_url": f"{ISSUER}/.well-known/openid-configuration",
        "client_id": CLIENT_ID,
        "client_secret": SecretStr(CLIENT_SECRET),
        "redirect_url": "https://dashboard.example/oidc/callback",
    }
    fields.update(overrides)
    return DashboardOidcConfig.model_validate(fields)


def _flow(double: ProviderDouble, config: DashboardOidcConfig | None = None) -> OidcFlow:
    return OidcFlow(
        config or _oidc_config(),
        client=httpx.AsyncClient(transport=double.transport()),
    )


def _app(flow: OidcFlow, **overrides) -> object:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    overrides.setdefault("permitted_origins", build_permitted(
        dashboard_host="127.0.0.1", dashboard_port=8090,
    ))
    overrides.setdefault("oidc", flow)
    return create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(), **overrides,
    )


def _login_redirect(client: TestClient) -> str:
    first = client.get("/", follow_redirects=False)
    assert first.status_code == 302, first.text
    assert urlparse(first.headers["location"]).path == "/oidc/login"
    second = client.get("/oidc/login", follow_redirects=False)
    assert second.status_code == 302, second.text
    return second.headers["location"]


def _state_cookie(client: TestClient) -> str:
    state = client.cookies.get("oidc_state")
    assert state, "login must plant the state cookie"
    return state


def test_unauthenticated_page_load_redirects_to_provider():
    double = ProviderDouble()
    with TestClient(_app(_flow(double)), base_url=BASE) as client:
        location = _login_redirect(client)
        parsed = urlparse(location)
        assert parsed.scheme == "https"
        assert parsed.netloc == "provider.test"
        query = parse_qs(parsed.query)
        assert query["response_type"] == ["code"]
        assert query["client_id"] == [CLIENT_ID]
        assert query["scope"] == ["openid"]
        assert query["redirect_uri"] == ["https://dashboard.example/oidc/callback"]
        assert query["state"] and query["state"][0]
        assert query["nonce"] and query["nonce"][0]


def test_callback_with_valid_code_establishes_session():
    double = ProviderDouble()
    code = double.issue_code()
    with TestClient(_app(_flow(double)), base_url=BASE) as client:
        nonce = parse_qs(urlparse(_login_redirect(client)).query)["nonce"][0]
        double.id_nonce = nonce
        response = client.get(
            f"/oidc/callback?code={code}&state={_state_cookie(client)}",
            follow_redirects=False,
        )
        assert response.status_code == 302, response.text
        assert response.headers["location"] == "/"
        assert response.cookies.get("coordinare_session")
        assert client.get("/").status_code == 200


def test_tampered_state_is_refused_without_session():
    double = ProviderDouble()
    code = double.issue_code()
    with TestClient(_app(_flow(double)), base_url=BASE) as client:
        _login_redirect(client)
        response = client.get(
            f"/oidc/callback?code={code}&state=tampered", follow_redirects=False,
        )
        assert response.status_code == 403
        assert response.cookies.get("coordinare_session") is None


def test_nonce_mismatch_is_refused():
    double = ProviderDouble()
    code = double.issue_code()
    with TestClient(_app(_flow(double)), base_url=BASE) as client:
        nonce = parse_qs(urlparse(_login_redirect(client)).query)["nonce"][0]
        double.id_nonce = f"not-{nonce}"
        response = client.get(
            f"/oidc/callback?code={code}&state={_state_cookie(client)}",
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert response.cookies.get("coordinare_session") is None


def test_expired_state_cookie_is_refused():
    double = ProviderDouble()
    code = double.issue_code()
    with TestClient(_app(_flow(double)), base_url=BASE) as client:
        _login_redirect(client)
        client.cookies.set("oidc_state", "stale-value")
        response = client.get(
            f"/oidc/callback?code={code}&state=stale-value", follow_redirects=False,
        )
        assert response.status_code == 403
        assert response.cookies.get("coordinare_session") is None


def test_replayed_code_is_refused():
    double = ProviderDouble()
    with TestClient(_app(_flow(double)), base_url=BASE) as client:
        # First round trip: redeem the code once, legitimately.
        first_nonce = parse_qs(urlparse(_login_redirect(client)).query)["nonce"][0]
        double.id_nonce = first_nonce
        first_code = double.issue_code()
        first = client.get(
            f"/oidc/callback?code={first_code}&state={_state_cookie(client)}",
            follow_redirects=False,
        )
        assert first.status_code == 302
        # Second login round trip (session held, so skip the page load):
        # presenting the FIRST code against a fresh state must fail at the
        # provider (403), not pass silently.
        second = client.get("/oidc/login", follow_redirects=False)
        assert second.status_code == 302
        second_nonce = parse_qs(urlparse(second.headers["location"]).query)["nonce"][0]
        double.id_nonce = second_nonce
        replay = client.get(
            f"/oidc/callback?code={first_code}&state={_state_cookie(client)}",
            follow_redirects=False,
        )
        assert replay.status_code == 403


def test_provider_unreachable_at_login_is_a_diagnostic_not_content():
    double = ProviderDouble()
    config = _oidc_config(discovery_url=f"{ISSUER}/missing-discovery")
    with TestClient(_app(_flow(double, config)), base_url=BASE) as client:
        response = client.get("/oidc/login", follow_redirects=False)
        assert response.status_code == 502, response.text
        assert CLIENT_SECRET not in response.text
        assert "provider" in response.text.lower() or "unavailable" in response.text.lower()


def test_provider_rejecting_client_credentials_is_refused():
    double = ProviderDouble()
    code = double.issue_code()
    config = _oidc_config(client_secret=SecretStr("wrong-secret-0123456789012345678"))
    with TestClient(_app(_flow(double, config)), base_url=BASE) as client:
        _login_redirect(client)
        response = client.get(
            f"/oidc/callback?code={code}&state={_state_cookie(client)}",
            follow_redirects=False,
        )
        assert response.status_code == 403, response.text
        assert "wrong-secret" not in response.text


class _HttpEndpointDouble(ProviderDouble):
    def discovery(self) -> dict[str, str]:
        document = super().discovery()
        document["token_endpoint"] = "http://provider.test/token"
        return document


class _MalformedJwksDouble(ProviderDouble):
    def public_jwks(self) -> dict[str, Any]:
        return {"keys": ["not-a-dict"]}


def test_cookies_are_secure_behind_tls_terminating_proxy():
    double = ProviderDouble()
    config = _oidc_config()
    with TestClient(_app(_flow(double, config)), base_url="http://127.0.0.1:8090") as client:
        response = client.get("/oidc/login", follow_redirects=False)
        assert response.status_code == 302, response.text
        set_cookies = response.headers.get_list("set-cookie")
        assert len(set_cookies) == 2
        for raw in set_cookies:
            assert "secure" in raw.lower()


def test_discovery_endpoints_must_be_https():
    with TestClient(_app(_flow(_HttpEndpointDouble())), base_url=BASE) as client:
        response = client.get("/oidc/login", follow_redirects=False)
        assert response.status_code == 502, response.text


def test_malformed_jwks_is_a_provider_error():
    double = _MalformedJwksDouble()
    code = double.issue_code()
    with TestClient(_app(_flow(double)), base_url=BASE) as client:
        nonce = parse_qs(urlparse(_login_redirect(client)).query)["nonce"][0]
        double.id_nonce = nonce
        response = client.get(
            f"/oidc/callback?code={code}&state={_state_cookie(client)}",
            follow_redirects=False,
        )
        assert response.status_code == 502, response.text

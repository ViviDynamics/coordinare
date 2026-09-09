"""All dashboard routes share the optional authentication boundary."""
from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard_auth import validate_dashboard_bind
from coordinare.localhost_guard import build_permitted

TOKEN = "a-secret-operator-token-of-at-least-32-characters"
BASE = "http://127.0.0.1:8090"


def _app(token=TOKEN):
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    return create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        auth_token=SecretStr(token) if token is not None else None,
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )


def test_all_mutating_routes_require_authentication_and_same_origin():
    app = _app()
    with TestClient(app, base_url=BASE) as client:
        tested = 0
        for route in app.routes:
            for method in getattr(route, "methods", set()) & {"POST", "PUT", "PATCH", "DELETE"}:
                path = route.path
                for key in getattr(route, "param_convertors", {}):
                    path = path.replace("{" + key + "}", "test")
                assert client.request(method, path).status_code == 401, (method, path)
                assert client.request(method, path, headers={"Authorization": "Bearer wrong"}).status_code == 401
                assert client.request(method, path, headers={
                    "Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example",
                }).status_code == 403
                tested += 1
        assert tested >= 10


@pytest.mark.parametrize("credential", [
    "Bearer wrong", "Basic not-base64!", "Basic " + base64.b64encode(b"no-colon").decode(),
    "Basic " + base64.b64encode(("wrong:" + TOKEN).encode()).decode(), "Digest unused",
])
def test_invalid_credentials_are_rejected_without_echo(credential):
    with TestClient(_app(), base_url=BASE) as client:
        response = client.get("/", headers={"Authorization": credential})
        assert response.status_code == 401
        assert TOKEN not in response.text
        assert "Basic" in response.headers["www-authenticate"]


@pytest.mark.parametrize("credential", [
    "Bearer " + TOKEN,
    "Basic " + base64.b64encode(("operator:" + TOKEN).encode()).decode(),
])
def test_browser_and_api_authentication(credential):
    with TestClient(_app(), base_url=BASE) as client:
        assert client.get("/", headers={"Authorization": credential}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/events", "/api/config/global", "/docs", "/openapi.json"])
def test_reads_and_sse_require_authentication(path):
    with TestClient(_app(), base_url=BASE) as client:
        assert client.get(path).status_code == 401


def test_local_default_is_unchanged():
    with TestClient(_app(None), base_url=BASE) as client:
        assert client.get("/").status_code == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.3", "dashboard.example"])
def test_remote_bind_needs_auth(host):
    with pytest.raises(ValueError, match="requires dashboard_auth_token"):
        validate_dashboard_bind(host, None)
    validate_dashboard_bind(host, SecretStr(TOKEN))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_can_remain_unauthenticated(host):
    validate_dashboard_bind(host, None)


def test_short_secret_error_does_not_reveal_value():
    with pytest.raises(ValueError) as exc:
        validate_dashboard_bind("127.0.0.1", SecretStr("sensitive-password"))
    assert "sensitive-password" not in str(exc.value)


def test_secret_descriptor_masks_token_and_requires_restart():
    from coordinare.config_descriptors import annotation_for, serialize_value
    annotation = annotation_for("global.dashboard_auth_token")
    assert annotation.secret and annotation.restart_required
    assert TOKEN not in str(serialize_value(TOKEN, secret=annotation.secret))


def test_signed_webhook_keeps_its_own_authentication():
    import asyncio
    import hashlib
    import hmac

    from coordinare.dashboard import register_webhook_route

    app = create_dashboard_app(
        DashboardStore(), MagicMock(), MagicMock(), MagicMock(),
        auth_token=SecretStr(TOKEN), guard_exempt_paths=frozenset({"/webhook"}),
        signed_webhook_paths=frozenset({"/webhook"}),
    )
    register_webhook_route(app, path="/webhook", secret="webhook-secret", trigger=asyncio.Event())
    with TestClient(app, base_url=BASE) as client:
        assert client.post("/webhook", content=b"{}", headers={"Host": "public.example"}).status_code == 401
        assert client.get("/webhook").status_code == 401
        signature = "sha256=" + hmac.new(b"webhook-secret", b"{}", hashlib.sha256).hexdigest()
        assert client.post("/webhook", content=b"{}", headers={
            "Host": "public.example", "X-Hub-Signature-256": signature,
        }).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_ip,expected_status", [("10.42.0.8", 200), ("192.0.2.10", 403)])
async def test_https_origin_requires_trusted_forwarded_scheme(proxy_ip, expected_status):
    from httpx import ASGITransport, AsyncClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app = _app()

    @app.post("/auth-probe")
    async def probe():
        return {"ok": True}

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=["10.42.0.8"])
    transport = ASGITransport(app=wrapped, client=(proxy_ip, 1234))
    async with AsyncClient(transport=transport, base_url=BASE) as client:
        response = await client.post("/auth-probe", headers={
            "Authorization": f"Bearer {TOKEN}",
            "Origin": "https://127.0.0.1:8090",
            "X-Forwarded-Proto": "https",
        })
    assert response.status_code == expected_status


def test_token_is_masked_in_real_config_api(temp_config_path):
    import yaml

    from tests.unit.test_dashboard_config_api import _make_client

    raw = yaml.safe_load(temp_config_path.read_text())
    raw["dashboard_auth_token"] = TOKEN
    temp_config_path.write_text(yaml.safe_dump(raw))
    client = _make_client(temp_config_path, raw)
    for path in ("/api/config/all", "/api/config/section/global", "/api/config/global"):
        response = client.get(path)
        assert response.status_code == 200
        assert TOKEN not in response.text


@pytest.mark.asyncio
async def test_startup_refuses_anonymous_remote_bind_before_bootstrap(monkeypatch):
    from types import SimpleNamespace

    import coordinare.__main__ as main

    def must_not_bootstrap():
        raise AssertionError("services bootstrapped before auth validation")

    monkeypatch.setattr(main, "CoordinareGraphBuilder", must_not_bootstrap)
    config = SimpleNamespace(dashboard_host="0.0.0.0", dashboard_auth_token=None)
    with pytest.raises(ValueError, match="requires dashboard_auth_token"):
        await main._run(config)


def test_localhost_guard_exemption_does_not_bypass_dashboard_auth():
    app = create_dashboard_app(
        DashboardStore(), MagicMock(), MagicMock(), MagicMock(),
        auth_token=SecretStr(TOKEN), guard_exempt_paths=frozenset({"/future-route"}),
    )
    with TestClient(app) as client:
        assert client.post("/future-route").status_code == 401

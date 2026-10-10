from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

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
    ("https://dashboard.example:0", 403),
    ("https://dashboard.example:not-a-port", 403),
    ("https://dashboard.example:65536", 403),
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


@pytest.mark.parametrize("port", ["not-a-port", "65536"])
def test_invalid_public_redirect_port_is_rejected_at_configuration_load(port: str) -> None:
    with pytest.raises(ValidationError, match=r"redirect_url.*valid port"):
        DashboardOidcConfig(
            discovery_url="https://identity.example/.well-known/openid-configuration",
            client_id="synthetic-client", client_secret=SecretStr("synthetic-secret"),
            redirect_url=f"https://dashboard.example:{port}/oidc/callback",
        )


@pytest.mark.parametrize("origin,status", [
    ("https://dashboard.example", 200),
    ("https://dashboard.example:443", 200),
    ("http://dashboard.example", 403),
    ("https://unrelated.example", 403),
    ("https://dashboard.example:not-a-port", 403),
])
def test_bearer_mutations_with_oidc_use_the_same_public_origin(origin: str, status: int) -> None:
    session_app, _session = authenticated_app()
    app = DashboardAuthentication(session_app.app, token=SecretStr("synthetic-bearer-token-32-characters"), oidc=session_app._oidc)
    with TestClient(app, base_url="http://dashboard.example") as client:
        response = client.post("/probe", headers={"Authorization": "Bearer synthetic-bearer-token-32-characters", "Origin": origin})
    assert response.status_code == status


@pytest.mark.parametrize("origin,status", [
    ("https://[2001:db8::1]:8443", 200),
    ("https://[2001:db8::1:8443]", 403),
])
def test_public_ipv6_address_and_port_do_not_collide_with_a_different_permitted_host(origin: str, status: int) -> None:
    from unittest.mock import MagicMock

    from coordinare.dashboard import DashboardStore, create_dashboard_app
    from coordinare.localhost_guard import build_permitted

    flow = OidcFlow(DashboardOidcConfig(
        discovery_url="https://identity.example/.well-known/openid-configuration",
        client_id="synthetic-client", client_secret=SecretStr("synthetic-secret"),
        redirect_url="https://[2001:db8::1]:8443/oidc/callback",
    ))
    session = flow.sessions.create("operator@example.test", lifetime_seconds=60)
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), oidc=flow,
                               permitted_origins=build_permitted("0.0.0.0", 80, ["2001:db8::1", "2001:db8::1:8443"]))

    @app.post("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/probe", headers={"Host": "[2001:db8::1]", "Cookie": f"coordinare_session={session}", "Origin": origin})
    assert response.status_code == status


def test_expanded_public_ipv6_redirect_accepts_compressed_browser_origin() -> None:
    from unittest.mock import MagicMock

    from coordinare.dashboard import DashboardStore, create_dashboard_app
    from coordinare.localhost_guard import build_permitted

    flow = OidcFlow(DashboardOidcConfig(
        discovery_url="https://identity.example/.well-known/openid-configuration",
        client_id="synthetic-client", client_secret=SecretStr("synthetic-secret"),
        redirect_url="https://[0:0:0:0:0:0:0:1]/oidc/callback",
    ))
    session = flow.sessions.create("operator@example.test", lifetime_seconds=60)
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), oidc=flow,
                               permitted_origins=build_permitted("::1", 80))

    @app.post("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app, base_url="https://localhost") as client:
        response = client.post("/probe", headers={"Host": "[::1]", "Cookie": f"coordinare_session={session}", "Origin": "https://[::1]"})
    assert response.status_code == 200


@pytest.mark.parametrize("host", ["v1.fe80::", "v1.example"])
def test_unsupported_ipvfuture_redirect_host_is_rejected_at_configuration_load(host: str) -> None:
    with pytest.raises(ValidationError, match=r"redirect_url.*IPvFuture"):
        DashboardOidcConfig(
            discovery_url="https://identity.example/.well-known/openid-configuration",
            client_id="synthetic-client", client_secret=SecretStr("synthetic-secret"),
            redirect_url=f"https://[{host}]/oidc/callback",
        )


def test_ordinary_dns_redirect_starting_with_v_remains_supported() -> None:
    config = DashboardOidcConfig(
        discovery_url="https://identity.example/.well-known/openid-configuration",
        client_id="synthetic-client", client_secret=SecretStr("synthetic-secret"),
        redirect_url="https://v1.example/oidc/callback",
    )
    assert config.redirect_url == "https://v1.example/oidc/callback"


def test_bracketed_ipvfuture_origin_does_not_match_an_ordinary_dns_redirect() -> None:
    from unittest.mock import MagicMock

    from coordinare.dashboard import DashboardStore, create_dashboard_app
    from coordinare.localhost_guard import build_permitted

    flow = OidcFlow(DashboardOidcConfig(
        discovery_url="https://identity.example/.well-known/openid-configuration",
        client_id="synthetic-client", client_secret=SecretStr("synthetic-secret"),
        redirect_url="https://v1.example/oidc/callback",
    ))
    session = flow.sessions.create("operator@example.test", lifetime_seconds=60)
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), oidc=flow,
                               permitted_origins=build_permitted("0.0.0.0", 80, ["v1.example"]))

    @app.post("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app, base_url="https://v1.example") as client:
        cookie = {"Cookie": f"coordinare_session={session}"}
        rejected = client.post("/probe", headers={**cookie, "Origin": "https://[v1.example]"})
        assert rejected.status_code == 403
        assert flow.sessions.lookup(session) is not None
        accepted = client.post("/probe", headers={**cookie, "Origin": "https://v1.example"})
        assert accepted.status_code == 200

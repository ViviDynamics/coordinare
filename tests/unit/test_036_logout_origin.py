from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import DashboardOidcConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard_oidc import OidcFlow
from coordinare.localhost_guard import build_permitted


@pytest.mark.parametrize("origin", ["https://dashboard.example:bad", "https://dashboard.example:65536"])
def test_malformed_logout_origin_is_rejected_and_legitimate_logout_still_revokes(origin: str) -> None:
    flow = OidcFlow(DashboardOidcConfig(
        discovery_url="https://identity.example/.well-known/openid-configuration",
        client_id="synthetic-client", client_secret=SecretStr("synthetic-secret"),
        redirect_url="https://dashboard.example/oidc/callback",
    ))
    session = flow.sessions.create("operator@example.test", lifetime_seconds=60)
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), oidc=flow,
                               permitted_origins=build_permitted("0.0.0.0", 80, ["dashboard.example"]))
    with TestClient(app, base_url="http://dashboard.example", raise_server_exceptions=False) as client:
        cookie = {"Cookie": f"coordinare_session={session}"}
        rejected = client.post("/oidc/logout", headers={**cookie, "Origin": origin}, follow_redirects=False)
        assert rejected.status_code == 403
        assert flow.sessions.lookup(session) is not None
        accepted = client.post("/oidc/logout", headers={**cookie, "Origin": "https://dashboard.example"}, follow_redirects=False)
        assert accepted.status_code == 204
        assert flow.sessions.lookup(session) is None

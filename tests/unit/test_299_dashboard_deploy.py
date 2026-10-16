from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from coordinare.dashboard import _DASHBOARD_JS_REVISIONS, DashboardStore, create_dashboard_app
from coordinare.dashboard.routers import stats
from coordinare.dashboard_oidc import OidcFlow
from coordinare.health import create_health_app
from coordinare.observability import HealthRegistry, HealthStatus
from coordinare.services.run_transcript import TranscriptCollector
from tests.unit.dashboard.oidc_double import ProviderDouble
from tests.unit.test_147_helm_deployment import _container, _one, _render
from tests.unit.test_497_oidc_login import _oidc_config
from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_metrics

RETRY_SECONDS = 2
GRACE_SECONDS = 45
DRAIN_SECONDS = 5


def _daemon() -> MagicMock:
    daemon = MagicMock()
    daemon._cycle_active = False
    daemon.running = False
    daemon.state = {"startup_complete": False}
    daemon.state_store = None
    daemon.shutdown = AsyncMock()
    return daemon


@pytest.mark.asyncio
async def test_starting_shell_assets_auth_and_state_gate() -> None:
    daemon = _daemon()
    token = "x" * 32
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), auth_token=SecretStr(token))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8090") as client:
        assert (await client.get("/api/state")).status_code == 401
        assert (await client.get("/dashboard/ready")).status_code == 200
        client.headers["Authorization"] = f"Bearer {token}"
        for path in ("/", "/config", "/cards/demo/card"):
            response = await client.get(path)
            assert response.status_code == 200
            assert "Coordinare is starting" in response.text
            assert response.headers["cache-control"] == "no-store"
        asset = await client.get("/static/app.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, no-cache"
        for method, path in (("GET", "/api/state"), ("GET", "/events"), ("POST", "/api/force-poll"), ("PUT", "/api/config/global")):
            response = await client.request(method, path)
            assert response.status_code == 503
            assert response.headers["retry-after"] == str(RETRY_SECONDS)
            assert response.json()["status"] == "starting"
        daemon.running = True
        assert (await client.get("/api/force-poll")).status_code == 503
        daemon.state["startup_complete"] = True
        assert (await client.post("/api/force-poll")).status_code == 202


@pytest.mark.asyncio
async def test_health_daemon_readiness_is_separate_from_dashboard() -> None:
    daemon = _daemon()
    health = HealthRegistry(timeout_seconds=RETRY_SECONDS)
    health.register("github", required=True)
    health.update("github", HealthStatus.healthy)
    app = create_health_app(daemon, health_registry=health)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://health") as client:
        assert (await client.get("/live")).json() == {"status": "alive"}
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/health")).json()["daemon_ready"] is False
        daemon.running = True
        assert (await client.get("/ready")).status_code == 503
        daemon.state["startup_complete"] = True
        assert (await client.get("/ready")).status_code == 200
        assert (await client.get("/health")).json()["daemon_ready"] is True
        health.update("github", HealthStatus.degraded)
        response = await client.get("/ready")
        assert response.status_code == 503 and response.json()["daemon_ready"] is False
        assert (await client.get("/health")).json()["daemon_ready"] is False


def test_chart_routes_early_and_drains_with_bounded_grace() -> None:
    objects = _render()
    container = _container(objects)
    probe = container["readinessProbe"]
    assert probe["httpGet"] == {"path": "/dashboard/ready", "port": "dashboard"}
    assert probe["periodSeconds"] == RETRY_SECONDS
    assert probe["initialDelaySeconds"] == 0
    assert probe["timeoutSeconds"] == 1
    assert container["livenessProbe"]["httpGet"] == {"path": "/live", "port": "health"}
    assert container["lifecycle"]["preStop"]["exec"]["command"] == ["/bin/sleep", str(DRAIN_SECONDS)]
    pod = _one(objects, "StatefulSet")["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] == GRACE_SECONDS
    from coordinare.__main__ import (
        EVENT_STORE_SHUTDOWN_SECONDS,
        HTTP_SHUTDOWN_SECONDS,
        RUNTIME_CANCEL_SECONDS,
        RUNTIME_SHUTDOWN_SECONDS,
    )
    from coordinare.daemon import SHUTDOWN_DRAIN_SECONDS, SHUTDOWN_SNAPSHOT_SECONDS

    assert GRACE_SECONDS > (
        DRAIN_SECONDS + HTTP_SHUTDOWN_SECONDS + RUNTIME_SHUTDOWN_SECONDS
        + RUNTIME_CANCEL_SECONDS + SHUTDOWN_DRAIN_SECONDS + SHUTDOWN_SNAPSHOT_SECONDS
        + 1 + EVENT_STORE_SHUTDOWN_SECONDS
    )
    customized = _render(terminationGracePeriodSeconds=60, startupDeadlineSeconds=700)
    assert _one(customized, "StatefulSet")["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 60
    import yaml
    cfg = yaml.safe_load(_one(customized, "ConfigMap")["data"]["config.yaml"])
    assert cfg["startup_deadline_seconds"] == 700


@pytest.mark.asyncio
async def test_oidc_login_and_session_work_during_startup() -> None:
    daemon = _daemon()
    double = ProviderDouble()
    provider = httpx.AsyncClient(transport=double.transport())
    flow = OidcFlow(_oidc_config(), client=provider)
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), oidc=flow)
    async with provider, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://127.0.0.1:8090",
    ) as client:
        assert (await client.get("/api/force-poll")).status_code == 401
        assert (await client.get("/")).headers["location"] == "/login"
        assert (await client.get("/login")).status_code == 200
        login = await client.get("/oidc/login")
        assert login.status_code == 302
        double.id_nonce = parse_qs(urlparse(login.headers["location"]).query)["nonce"][0]
        callback = await client.get("/oidc/callback", params={
            "code": double.issue_code(), "state": client.cookies.get("oidc_state"),
        })
        assert callback.status_code == 302
        assert client.cookies.get("coordinare_session")
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/api/account")).status_code == 200
        assert (await client.post("/api/force-poll")).json()["status"] == "starting"


@pytest.mark.asyncio
async def test_dashboard_probe_allows_kubelet_host_without_exposing_state() -> None:
    app = create_dashboard_app(
        DashboardStore(), _daemon(), MagicMock(), MagicMock(), auth_token=SecretStr("x" * 32),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://10.0.0.10:8090") as client:
        assert (await client.get("/dashboard/ready")).json() == {"status": "ready"}
        assert (await client.get("/api/account")).status_code == 403
        assert (await client.post("/dashboard/ready")).status_code == 401


@pytest.mark.asyncio
async def test_statistics_resolves_services_after_startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    daemon = _daemon()
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock())
    collector = MagicMock(spec=TranscriptCollector)
    collector.root = tmp_path
    collector.enabled = True
    daemon.state.update(startup_complete=True, transcript_collector=collector)
    daemon.running = True
    service = MagicMock()
    service.root = tmp_path
    service.event_path = None
    service.query = AsyncMock(return_value={"late_bound": True})
    factory = MagicMock(return_value=service)
    monkeypatch.setattr(stats, "RunStatistics", factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8090") as client:
        for _ in range(2):
            assert (await client.get("/api/stats")).json() == {"late_bound": True}
    factory.assert_called_once_with(tmp_path, event_path=None)


@pytest.mark.asyncio
async def test_only_matching_script_hashes_get_immutable_cache() -> None:
    app = create_dashboard_app(DashboardStore(), _daemon(), MagicMock(), MagicMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8090") as client:
        response = await client.get(f"/static/app.js?v={_DASHBOARD_JS_REVISIONS['app']}")
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
        for path in ("/static/app.js", "/static/app.js?v=old", "/static/shell.css", "/static/activity-streams.js"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "public, no-cache"


def test_snapshot_readiness_tracks_startup_and_required_health() -> None:
    daemon = _make_mock_daemon()
    health = HealthRegistry(timeout_seconds=RETRY_SECONDS)
    health.register("github", required=True)
    health.update("github", HealthStatus.healthy)
    store = DashboardStore()
    daemon.state["startup_complete"] = False
    assert store.build_snapshot(daemon, _make_mock_metrics(), health)["daemon_ready"] is False
    daemon.state["startup_complete"] = True
    assert store.build_snapshot(daemon, _make_mock_metrics(), health)["daemon_ready"] is True
    health.update("github", HealthStatus.degraded)
    assert store.build_snapshot(daemon, _make_mock_metrics(), health)["daemon_ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state,running,expected", [
    ({}, True, "starting"),
    ({"startup_complete": True, "stopping": True}, False, "stopping"),
    ({"startup_complete": True}, False, "unavailable"),
])
async def test_gate_defaults_closed_and_reports_reason(state, running, expected) -> None:
    daemon = _daemon()
    daemon.state = state
    daemon.running = running
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8090") as client:
        response = await client.get("/api/state")
    assert response.status_code == 503
    assert response.json()["status"] == expected
    assert expected in response.json()["message"].lower()
    assert ("retry-after" in response.headers) == (expected != "unavailable")


@pytest.mark.asyncio
async def test_shell_and_activity_urls_match_served_content_hashes() -> None:
    import hashlib
    import re

    app = create_dashboard_app(DashboardStore(), _daemon(), MagicMock(), MagicMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8090") as client:
        page = (await client.get("/")).text
        for name in ("shell.css", "activity-streams.js"):
            match = re.search(r'/static/' + re.escape(name) + r'\?v=([a-f0-9]+)', page)
            assert match, name
            response = await client.get(match.group(0))
            assert hashlib.sha256(response.content).hexdigest().startswith(match.group(1))
            assert response.headers["cache-control"] == "public, max-age=31536000, immutable"

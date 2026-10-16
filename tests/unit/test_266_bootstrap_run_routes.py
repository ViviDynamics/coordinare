"""Bootstrap actions use the dashboard's authentication and origin guards."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from coordinare.config import DashboardOidcConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard_oidc import SESSION_COOKIE, OidcFlow, OidcIdentity
from coordinare.models.env_cache import EnvCacheState
from coordinare.services import event_store_query
from coordinare.services.bootstrap_history import BootstrapHistory
from coordinare.services.bootstrap_runs import BootstrapRuntime, load_bootstrap_history
from coordinare.services.env_cache import EnvCacheService
from coordinare.services.event_store import EventStore
from tests.unit.test_060_env_cache import _make_daemon_for_env_cache
from tests.unit.test_266_bootstrap_runs import RUN_COUNT, _start
from tests.unit.test_270_bootstrap_control import BASE, TOKEN, _trigger_daemon
from tests.unit.test_dashboard import _make_mock_health, _make_mock_metrics


@pytest.fixture(autouse=True)
def _patient_event_store_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests check what is recorded, not how fast it reads back.

    Dashboard reads give up after 250 ms (50 ms waiting on the writer's lock) so a page never
    stalls. A loaded CI runner can exceed that, and the read then fails as "history
    unavailable", which flaked these tests in unrelated PRs. The budget itself has its own
    tests (test_182_run_statistics.py, test_182_review_fixes.py).
    """
    monkeypatch.setattr(event_store_query, "QUERY_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(event_store_query, "READ_BUSY_TIMEOUT_MS", 10_000)


CANCEL_PATH = "/api/symphonies/alpha/env-bootstrap/cancel"


@pytest.mark.asyncio
async def test_cancel_auth_origin_conflict_and_actor() -> None:
    daemon = _trigger_daemon(in_flight=True)
    daemon.cancel_env_bootstrap = AsyncMock(return_value={"status": "cancelled", "stopped": True})
    app = create_dashboard_app(DashboardStore(), daemon, _make_mock_metrics(), _make_mock_health(),
                               auth_token=SecretStr(TOKEN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        assert (await client.post(CANCEL_PATH)).status_code == 401
        headers = {"Authorization": f"Bearer {TOKEN}"}
        refused = await client.post(CANCEL_PATH, headers={**headers, "Origin": "https://evil.example"})
        assert refused.status_code == 403
        daemon.cancel_env_bootstrap.assert_not_awaited()
        response = await client.post(CANCEL_PATH, headers=headers)
        assert response.status_code == 200
        daemon.cancel_env_bootstrap.assert_awaited_once_with("alpha", "token")
        daemon.cancel_env_bootstrap.side_effect = ValueError("No bootstrap is running")
        assert (await client.post(CANCEL_PATH, headers=headers)).status_code == 409


@pytest.mark.asyncio
async def test_run_page_and_api_do_not_cross_symphonies() -> None:
    daemon = _trigger_daemon()
    app = create_dashboard_app(DashboardStore(), daemon, _make_mock_metrics(), _make_mock_health())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        assert (await client.get("/symphonies/alpha/bootstrap/run-one")).status_code == 200
        assert (await client.get("/api/symphonies/alpha/bootstrap-runs/run-one")).status_code == 404
        assert (await client.get("/api/symphonies/missing/bootstrap-runs/run-one")).status_code == 404


@pytest.mark.asyncio
async def test_cancel_session_csrf_and_named_actor() -> None:
    daemon = _trigger_daemon(in_flight=True)
    daemon.cancel_env_bootstrap = AsyncMock(return_value={"status": "cancelled", "stopped": True})
    flow = OidcFlow(DashboardOidcConfig(
        discovery_url="https://issuer.example/.well-known/openid-configuration", client_id="test-client",
        client_secret=SecretStr("test-secret"), redirect_url=BASE + "/oidc/callback",
    ))
    cookie, _ = flow.start_session(OidcIdentity(subject="ada", name="Ada Lovelace"))
    app = create_dashboard_app(DashboardStore(), daemon, _make_mock_metrics(), _make_mock_health(), oidc=flow)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                                 cookies={SESSION_COOKIE: cookie}) as client:
        response = await client.post(CANCEL_PATH, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
        daemon.cancel_env_bootstrap.assert_not_awaited()
        response = await client.post(CANCEL_PATH, headers={"Origin": BASE})
        assert response.status_code == 200
        daemon.cancel_env_bootstrap.assert_awaited_once_with("alpha", "Ada Lovelace")


@pytest.mark.asyncio
async def test_cancel_endpoint_closes_and_persists_the_real_history(tmp_path) -> None:
    daemon = _make_daemon_for_env_cache()
    daemon.state["symphony_configs"] = _trigger_daemon().state["symphony_configs"]
    daemon.state["env_cache"]["alpha"] = EnvCacheState(symphony_name="alpha", sanitised_name="alpha",
                                                      cache_dir=tmp_path, bootstrap_in_flight=True)
    daemon.state["env_cache_service"] = EnvCacheService(daemon.state["coordinare_config"])
    store = DashboardStore()
    daemon.state["activity_log"] = store.activity_log
    events = EventStore(tmp_path / "events.db")
    await events.start()
    daemon.state["event_store"] = events
    run = _start(daemon.state)
    owner = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=True))
    task = asyncio.create_task(asyncio.Event().wait())
    daemon.state["bootstrap_runtimes"] = {"alpha": BootstrapRuntime(owner, "job-alpha", task)}
    app = create_dashboard_app(store, daemon, _make_mock_metrics(), _make_mock_health())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
            response = await client.post(CANCEL_PATH)
            assert response.status_code == 200
            assert response.json()["stopped"] is True
            assert (await client.post(CANCEL_PATH)).status_code == 409
            detail = await client.get(f"/api/symphonies/alpha/bootstrap-runs/{run.run_id}")
            assert detail.status_code == 200
            assert detail.json()["history"]["outcome"] == "cancelled"
            assert detail.json()["cancelled_by"] == "local"
            assert detail.json()["reason"] == "cancelled by local"
        await events.flush()
        daemon.state["bootstrap_history"] = BootstrapHistory()
        await load_bootstrap_history(daemon.state, ["alpha"])
        history = daemon.state["bootstrap_history"].recent("alpha", RUN_COUNT)
        assert len(history) == 1
        assert history[0].reason == "cancelled by local"
        assert history[0].outcome == "cancelled"
        assert history[0].cancelled_by == "local"
        owner.stop_session_confirmed.assert_awaited_once_with("job-alpha")
    finally:
        task.cancel()
        await events.close()

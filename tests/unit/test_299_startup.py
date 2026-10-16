from __future__ import annotations

import asyncio
import signal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from coordinare import __main__ as app_main
from coordinare.config import ProjectConfiguration
from coordinare.http_server import HTTP_DRAIN_SECONDS, ManagedServer
from tests.unit.test_299_dashboard_deploy import _daemon

WAIT_SECONDS = 2
TOKEN = "x" * 32


@pytest.mark.asyncio
async def test_listeners_precede_preflight_and_sigterm_drains_before_stopping_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProjectConfiguration(github_org="example", human_reviewers=["operator"], github_token=TOKEN)
    daemon = _daemon()
    monkeypatch.setattr(app_main, "CoordinareDaemon", MagicMock(return_value=daemon))
    monkeypatch.setattr(app_main, "StateStore", MagicMock())
    monkeypatch.setattr(app_main, "check_port_available", MagicMock())
    monkeypatch.setattr(app_main, "load_unexpanded_config", MagicMock(return_value={}))
    bootstrap = AsyncMock()
    monkeypatch.setattr(app_main, "_bootstrap_services", bootstrap)
    preflight_entered = asyncio.Event()
    preflight_block = asyncio.Event()
    request_finished = asyncio.Event()
    response_completed = asyncio.Event()
    request_entered = asyncio.Event()
    servers: list[Any] = []
    callbacks: dict[signal.Signals, Any] = {}

    class FakeServer:
        def __init__(self, server_config: Any) -> None:
            self.config = server_config
            self.started = False
            self.exit_event = asyncio.Event()
            self.draining = asyncio.Event()
            servers.append(self)

        @property
        def should_exit(self) -> bool:
            return self.exit_event.is_set()

        @should_exit.setter
        def should_exit(self, value: bool) -> None:
            if value:
                self.exit_event.set()

        async def serve(self) -> None:
            self.started = True
            await self.exit_event.wait()
            self.draining.set()
            if self.config.port == config.dashboard_port:
                await response_completed.wait()

    async def preflight(*args: Any) -> None:
        assert len(servers) == 2 and all(server.started for server in servers)
        preflight_entered.set()
        await preflight_block.wait()

    monkeypatch.setattr(app_main, "ManagedServer", FakeServer)
    monkeypatch.setattr(app_main, "run_preflight", preflight)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: callbacks.update({sig: callback}))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)
    runtime = asyncio.create_task(app_main._run_with_events(config, None, None, "legacy", MagicMock()))
    try:
        await asyncio.wait_for(preflight_entered.wait(), WAIT_SECONDS)
        dashboard = next(server for server in servers if server.config.port == config.dashboard_port)
        assert dashboard.config.timeout_graceful_shutdown == HTTP_DRAIN_SECONDS
        @dashboard.config.app.get("/drain")
        async def in_flight_request() -> dict[str, str]:
            request_entered.set()
            await request_finished.wait()
            return {"status": "finished"}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard.config.app), base_url="http://127.0.0.1:8090",
        ) as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/api/force-poll")).status_code == 503
            request = asyncio.create_task(client.get("/drain"))
            await asyncio.wait_for(request_entered.wait(), WAIT_SECONDS)
            callbacks[signal.SIGTERM]()
            await asyncio.wait_for(dashboard.draining.wait(), WAIT_SECONDS)
            daemon.stop.assert_not_called()
            request_finished.set()
            assert (await asyncio.wait_for(request, WAIT_SECONDS)).json() == {"status": "finished"}
            response_completed.set()
        await asyncio.wait_for(runtime, WAIT_SECONDS)
        daemon.stop.assert_called_once()
        bootstrap.assert_not_awaited()
    finally:
        request_finished.set()
        response_completed.set()
        if not runtime.done():
            runtime.cancel()
            await asyncio.gather(runtime, return_exceptions=True)


def test_http_servers_leave_signal_ownership_to_process(monkeypatch: pytest.MonkeyPatch) -> None:
    signal_handler = MagicMock()
    monkeypatch.setattr(signal, "signal", signal_handler)
    server = ManagedServer(MagicMock())
    with server.capture_signals():
        signal_handler.assert_not_called()

"""Spec 551 T022: state tool contract against the real dashboard app.

``get_state`` returns the same snapshot the SSE stream builds; the state route
reuses ``build_snapshot`` so the two cannot disagree. ``get_performer_logs``
returns the buffered head of the stream endpoint; ``get_activity`` returns
newest-first entries narrowed by the optional ``card_id``.
"""

from __future__ import annotations

import socket
import threading
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.mcp.client import DashboardClient
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.tools.state_tools import (
    GetActivityInput,
    get_activity_handler,
    get_performer_logs_handler,
    get_state_handler,
)


def _config(dashboard_url: str | None = None) -> McpServerConfig:
    kwargs: dict[str, Any] = {}
    if dashboard_url is not None:
        kwargs["dashboard_url"] = dashboard_url
    return McpServerConfig(
        agent_token=SecretStr("a" * 32),
        dashboard_token=SecretStr("d" * 32),
        **kwargs,
    )


def _asgi_client(store: DashboardStore, daemon: MagicMock) -> DashboardClient:
    app = create_dashboard_app(store, daemon, MagicMock(), MagicMock())
    return DashboardClient(_config(), transport=httpx.ASGITransport(app=app))


class _Server:
    """uvicorn in a daemon thread for an ASGI app (stream endpoints need it)."""

    def __init__(self, app: Any) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = int(sock.getsockname()[1])
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"),
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            threading.Event().wait(0.1)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _daemon(phase: str = "idle", **state_extra: Any) -> MagicMock:
    daemon = MagicMock()
    daemon.state = {"phase": phase, "error_count": 0, **state_extra}
    daemon.state_store = None
    daemon._cycle_active = False
    daemon.running = True
    return daemon


@pytest.mark.asyncio
async def test_get_state_returns_snapshot_shape() -> None:
    store = DashboardStore()
    client = _asgi_client(store, _daemon(phase="working"))
    result = await get_state_handler(client)
    assert result.ok, result.error
    snapshot = result.data or {}
    assert snapshot.get("phase") == "working"
    assert "subsystems" in snapshot and "active_session_count" in snapshot


@pytest.mark.asyncio
async def test_get_performer_logs_returns_buffered_head() -> None:
    """The stream endpoint never ends; the tool must return its buffered head."""
    daemon = _daemon(agent_service=_log_service(["line one", "line two"]))
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock())
    with _Server(app) as server:
        client = DashboardClient(_config(server.url()))
        result = await get_performer_logs_handler(client)
    assert result.ok, result.error
    text = (result.data or {}).get("text", "")
    assert "line one" in text and "line two" in text


def _log_service(lines: list[str]) -> MagicMock:
    service = MagicMock()
    service.get_agent_logs = lambda: lines
    return service


@pytest.mark.asyncio
async def test_get_activity_newest_first_and_card_filter() -> None:
    store = DashboardStore()
    store.activity_log.record(
        activity_type="agent_event", card_id="card-a", text="first",
    )
    store.activity_log.record(
        activity_type="agent_event", card_id="card-a", text="second",
    )
    store.activity_log.record(
        activity_type="agent_event", card_id="card-b", text="other card",
    )
    client = _asgi_client(store, _daemon())

    all_result = await get_activity_handler(GetActivityInput(), client)
    assert all_result.ok
    entries = (all_result.data or {}).get("entries", [])
    texts = [e["text"] for e in entries]
    assert texts == ["first", "second", "other card"], texts

    narrowed = await get_activity_handler(GetActivityInput(card_id="card-a"), client)
    texts = [e["text"] for e in (narrowed.data or {}).get("entries", [])]
    assert texts == ["first", "second"], texts

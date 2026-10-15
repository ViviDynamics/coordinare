"""Phase 2 contracts, exercised in process without binding sockets."""
from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr, ValidationError

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard.context import DashboardContext
from coordinare.dashboard.routers import events
from coordinare.dashboard.sse import SSEBroadcaster
from coordinare.mcp.client import ToolResult
from coordinare.mcp.tools import find_tool
from coordinare.mcp.tools.event_tools import query_events_handler
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_params import EventQuery

BAD_PARAMS = [
    {"limit": "0"}, {"limit": "501"}, {"limit": "x"},
    {"start": "yesterday"}, {"start": "2026-10-01T00:00:00"},
    {"start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
    {"cursor": "broken"}, {"cursor": "x" * 513}, {"text": "x" * 1025},
    {"source": "other"}, {"level": "other"}, {"source": "info"}, {"level": "daemon"}, {"card_id": "x" * 1025},
]
SECRET = "ghp_" + "a" * 36
PAGE_SIZE = 1


def _app(store: object) -> FastAPI:
    app = FastAPI()
    ctx = DashboardContext(
        daemon=SimpleNamespace(state={"event_store": store}),
        store=SimpleNamespace(broadcaster=SSEBroadcaster()), metrics=None,
        health=None, config_path=None, assistant_on=False,
    )
    events.register(app, ctx)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("params", BAD_PARAMS)
async def test_api_rejects_invalid_parameters(params: dict[str, str]) -> None:
    store = SimpleNamespace(query_events=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(store)), base_url="http://test") as client:
        response = await client.get("/api/events", params=params)
    assert response.status_code == 422
    store.query_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_redacted_scoped_pages(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    try:
        for symphony in ("alpha", "beta", "beta"):
            store.emit({"symphony": symphony, "message": SECRET, "data": {"token": SECRET}})
        await store.flush()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(store)), base_url="http://test") as client:
            first = await client.get("/api/events", params={"symphony": "beta", "limit": PAGE_SIZE})
            body = first.json()
            second = await client.get("/api/events", params={"symphony": "beta", "limit": PAGE_SIZE, "cursor": body["cursor"]})
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert body["cursor"]
        assert second.json()["cursor"] is None
        rows = body["events"] + second.json()["events"]
        assert len({row["id"] for row in rows}) == len(rows)
        assert all(row["symphony"] == "beta" for row in rows)
        assert SECRET not in json.dumps(rows)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_response_redacts_even_legacy_rows() -> None:
    store = SimpleNamespace(query_events=AsyncMock(return_value={"events": [{"message": SECRET, "data": {"token": "hidden"}}], "cursor": None}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(store)), base_url="http://test") as client:
        response = await client.get("/api/events")
    assert SECRET not in response.text
    assert "hidden" not in response.text


@pytest.mark.asyncio
async def test_missing_store_has_safe_unavailable_response() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(None)), base_url="http://test") as client:
        response = await client.get("/api/events")
    assert response.status_code == 503
    assert "events.db" not in response.text


@pytest.mark.asyncio
async def test_commit_notification_follows_persistence(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    broadcaster = SSEBroadcaster()
    queue = broadcaster.subscribe()
    store.on_commit = lambda: broadcaster.broadcast({"_event": "events_changed"})
    await store.start()
    try:
        store.emit({"message": "committed"})
        await store.flush()
        assert queue.get_nowait() == {"_event": "events_changed"}
        assert (await store.query_events())["events"][0]["message"] == "committed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_notification_failure_does_not_retry_committed_rows(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    try:
        with patch.object(store, "on_commit", side_effect=RuntimeError("disconnected")):
            store.emit({"message": "once"})
            await store.flush()
        assert len((await store.query_events())["events"]) == 1
        assert store.health()["dropped"] == 0
    finally:
        await store.close()


@pytest.mark.parametrize("params", BAD_PARAMS)
def test_mcp_uses_the_same_parameter_validation(params: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        EventQuery.model_validate(params)


@pytest.mark.asyncio
async def test_mcp_forwards_all_filters_with_read_budget() -> None:
    params = EventQuery(
        start="2026-10-01T00:00:00Z", end="2026-10-02T00:00:00Z",
        symphony="alpha", card_id="card&1", run_id="run1", stage="qa",
        source="performer", kind="tool", level="info", text="two words", limit=PAGE_SIZE,
    )
    client = SimpleNamespace(get_json=AsyncMock(return_value=ToolResult(ok=True, data={"events": [], "cursor": None})), read_budget=7.0)
    result = await query_events_handler(params, client)
    assert result.ok
    path = client.get_json.call_args.args[0]
    assert urlsplit(path).path == "/api/events"
    assert parse_qs(urlsplit(path).query) == {key: [str(value)] for key, value in params.model_dump(exclude_none=True).items()}
    assert client.get_json.call_args.kwargs == {"timeout_seconds": client.read_budget}
    tool = find_tool("query_events")
    assert tool is not None
    assert tool.annotations.read_only_hint
    assert tool.rest_calls == (("GET", "/api/events"),)


@pytest.mark.asyncio
async def test_event_reads_do_not_generate_info_events() -> None:
    app = create_dashboard_app(DashboardStore(), MagicMock(), MagicMock(), MagicMock())
    with patch("coordinare.dashboard.app._log") as logger:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8090") as client:
            await client.get("/api/events", params={"limit": "0"})
    logger.info.assert_not_called()
    logger.debug.assert_called_once()


@pytest.mark.asyncio
async def test_events_route_keeps_dashboard_authentication() -> None:
    app = create_dashboard_app(DashboardStore(), MagicMock(), MagicMock(), MagicMock(), auth_token=SecretStr("dashboard-secret"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8090") as client:
        response = await client.get("/api/events")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_registered_mcp_tool_has_query_schema_and_invokes_authenticated_client() -> None:
    server = MCPServer("test-events")
    tool = find_tool("query_events")
    assert tool is not None
    server.add_tool(tool.handler, name=tool.name)
    client = SimpleNamespace(get_json=AsyncMock(return_value=ToolResult(ok=True, data={"events": [], "cursor": None})), read_budget=7.0)
    with patch("coordinare.mcp.tools.event_tools.get_client", return_value=client):
        result = await server.call_tool("query_events", {"query": {"symphony": "alpha", "limit": PAGE_SIZE}})
    assert result
    assert "symphony=alpha" in client.get_json.call_args.args[0]
    schema = (await server.list_tools())[0].input_schema
    assert "query" in schema["properties"]


@pytest.mark.asyncio
async def test_endpoint_forwards_every_filter_and_cursor() -> None:
    cursor = base64.urlsafe_b64encode(json.dumps(["2026-10-01T00:00:00Z", 1]).encode()).decode()
    values = {
        "start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z",
        "symphony": "beta", "card_id": "card", "run_id": "run", "stage": "qa",
        "source": "performer", "kind": "tool", "level": "error", "text": "failure",
        "cursor": cursor, "limit": PAGE_SIZE,
    }
    params = EventQuery.model_validate(values)
    store = SimpleNamespace(query_events=AsyncMock(return_value={"events": [], "cursor": None}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(store)), base_url="http://test") as client:
        response = await client.get("/api/events", params=values)
    assert response.status_code == 200
    store.query_events.assert_awaited_once_with(params.filters(), cursor, PAGE_SIZE)


@pytest.mark.asyncio
async def test_storage_error_does_not_expose_exception_text() -> None:
    store = SimpleNamespace(query_events=AsyncMock(side_effect=sqlite3.OperationalError(SECRET)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(store)), base_url="http://test") as client:
        response = await client.get("/api/events")
    assert response.status_code == 503
    assert SECRET not in response.text

"""Persistent event queries and commit notifications for the Logs page."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Annotated

import fastapi
from fastapi.responses import JSONResponse

from coordinare.lib.redaction import redact_mapping, redact_state_value
from coordinare.services.event_store_params import EventQuery

if TYPE_CHECKING:
    from coordinare.dashboard.context import DashboardContext

NO_STORE = {"Cache-Control": "no-store"}
UNAVAILABLE_STATUS = 503
EventQueryParams = Annotated[EventQuery, fastapi.Query()]


def register(app: fastapi.FastAPI, ctx: DashboardContext) -> None:
    event_store = ctx.daemon.state.get("event_store")
    if event_store is not None:
        # Called on the event loop only after a successful commit. No event data
        # travels here, so each browser re-queries using its own display scope.
        event_store.on_commit = lambda: ctx.store.broadcaster.broadcast({"_event": "events_changed"})

    @app.get("/api/events")
    async def query_events(params: EventQueryParams) -> JSONResponse:
        store = ctx.daemon.state.get("event_store")
        if store is None:
            return JSONResponse({"error": "event history unavailable"}, status_code=UNAVAILABLE_STATUS, headers=NO_STORE)
        try:
            # EventStore.query_events owns asyncio.to_thread and its read-only connection.
            body = await store.query_events(params.filters(), params.cursor, params.limit)
        except (sqlite3.Error, OSError):
            return JSONResponse({"error": "event history unavailable"}, status_code=UNAVAILABLE_STATUS, headers=NO_STORE)
        return JSONResponse(redact_mapping(redact_state_value(body)), headers=NO_STORE)

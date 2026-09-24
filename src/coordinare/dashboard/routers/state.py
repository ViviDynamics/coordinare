"""State, SSE and lifecycle action routes (436)."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from fastapi import (
    FastAPI,  # noqa: TC002 — FastAPI resolves handler signatures at registration; runtime import required
)
from fastapi.responses import JSONResponse, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from coordinare.dashboard.context import DashboardContext


_log = structlog.get_logger(__name__)


def _register_performer_logs_stream(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.get("/api/performer-logs")
        async def performer_logs_stream() -> StreamingResponse:
            """Stream the active performer's stderr log buffer, then tail new lines.

            Sends all buffered lines immediately, then polls every second and pushes
            any new lines until the client disconnects.  Plain text, one line per row.
            """
            async def _generate() -> AsyncGenerator[str, None]:
                agent_service = daemon.state.get("agent_service")
                getter = getattr(agent_service, "get_agent_logs", None)
                if not callable(getter):
                    yield "no performer active (agent_service does not support log buffering)\n"
                    return
                sent = 0
                while True:
                    logs: list[str] = getter()
                    new = logs[sent:]
                    for line in new:
                        yield line + "\n"
                    sent = len(logs)
                    await asyncio.sleep(1.0)

            return StreamingResponse(_generate(), media_type="text/plain")


def _register_force_poll(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/force-poll")
        async def force_poll() -> JSONResponse:
            """Trigger an immediate board poll cycle (016-force-poll).

            Returns 202 and fires the daemon's webhook_trigger when idle.
            Returns 409 when a cycle is already in progress.
            """
            if daemon._cycle_active:
                return JSONResponse(
                        {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                        status_code=409,
                    )
            daemon._webhook_trigger.set()
            return JSONResponse({"status": "accepted"}, status_code=202)


def _register_cancel_card(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/cancel")
        async def cancel_card() -> JSONResponse:
            """Cancel the currently active card (026-card-cancellation).

            Stops the performer, cleans up workspace, moves card to TODO.
            Returns 200 with cancellation result. Returns 409 if a cycle is active.
            """
            if daemon._cycle_active:
                return JSONResponse(
                        {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                        status_code=409,
                    )

            from coordinare.cancel import cancel_active_card

            result = await cancel_active_card(daemon.state)
            return JSONResponse(result)


def _register_sse_events(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        store = ctx.store
        metrics = ctx.metrics
        health = ctx.health

        @app.get("/events")
        async def sse_events() -> StreamingResponse:
            return StreamingResponse(
                store.sse_stream(daemon, metrics, health),
                media_type="text/event-stream",
            )


def register(app: FastAPI, ctx: DashboardContext) -> None:
    _register_performer_logs_stream(app, ctx)
    _register_force_poll(app, ctx)
    _register_cancel_card(app, ctx)
    _register_sse_events(app, ctx)

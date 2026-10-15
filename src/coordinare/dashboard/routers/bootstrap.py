"""Bootstrap run pages and cancellation, protected by the dashboard's shared guards."""
from __future__ import annotations

from typing import TYPE_CHECKING

import fastapi
from fastapi.responses import HTMLResponse, JSONResponse

from coordinare.dashboard.assets import _page_html
from coordinare.dashboard.routers.personas import _session_actor
from coordinare.lib.redaction import redact_state_value
from coordinare.services.bootstrap_runs import read_bootstrap_run

if TYPE_CHECKING:
    from fastapi import FastAPI

    from coordinare.dashboard.context import DashboardContext

NO_STORE = {"Cache-Control": "no-store"}


def register(app: FastAPI, ctx: DashboardContext) -> None:
    @app.get("/symphonies/{name}/bootstrap/{run}", response_class=HTMLResponse)
    async def bootstrap_page(name: str, run: str) -> HTMLResponse:
        return HTMLResponse(_page_html(ctx.assistant_on))

    @app.get("/api/symphonies/{name}/bootstrap-runs/{run}")
    async def bootstrap_run(name: str, run: str) -> JSONResponse:
        configs = ctx.daemon.state.get("symphony_configs") or {}
        detail = await read_bootstrap_run(ctx.daemon.state, name, run) if name in configs else None
        if detail is None:
            return JSONResponse({"error": "Bootstrap run not found in this symphony"},
                                status_code=fastapi.status.HTTP_404_NOT_FOUND, headers=NO_STORE)
        return JSONResponse(redact_state_value(detail), headers=NO_STORE)

    @app.post("/api/symphonies/{name}/env-bootstrap/cancel")
    async def cancel_run(name: str, request: fastapi.Request) -> JSONResponse:
        if name not in (ctx.daemon.state.get("symphony_configs") or {}):
            return JSONResponse({"error": "Symphony not found"}, status_code=fastapi.status.HTTP_404_NOT_FOUND)
        try:
            result = await ctx.daemon.cancel_env_bootstrap(name, _session_actor(request))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=fastapi.status.HTTP_409_CONFLICT)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=fastapi.status.HTTP_503_SERVICE_UNAVAILABLE)
        return JSONResponse(result)

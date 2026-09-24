"""Dashboard page and static-asset routes (436)."""
from __future__ import annotations

import asyncio  # noqa: F401
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

from coordinare.dashboard.assets import _ACTIVITY_STREAM_JS, _DASHBOARD_JS_SOURCES, _page_html

if TYPE_CHECKING:
        from coordinare.dashboard.context import DashboardContext

_log = structlog.get_logger(__name__)


def _register_activity_stream_script(app: FastAPI, ctx: DashboardContext) -> None:

        @app.get("/static/activity-streams.js")
        async def activity_stream_script() -> Response:
            return Response(_ACTIVITY_STREAM_JS, media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})


def _register_dashboard_static_script(app: FastAPI, ctx: DashboardContext) -> None:

        @app.get("/static/{filename}")
        async def dashboard_static_script(filename: str) -> Response:
            """349: content-addressed dashboard JS.

            The ``?v=`` query in the page is the sha256 of the served bytes, so
            this can be cached as immutable: a redeploy that changes a file
            changes the URL and no browser renders a stale script.
            """
            source = _DASHBOARD_JS_SOURCES.get(filename.removesuffix(".js"))
            if source is None:
                raise HTTPException(status_code=404)
            return Response(
                source,
                media_type="application/javascript; charset=utf-8",
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )


def _register_dashboard(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/", response_class=HTMLResponse)
        async def dashboard() -> HTMLResponse:
            return HTMLResponse(_page_html(_assistant_on))


def _register_dashboard_performers(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/performers", response_class=HTMLResponse)
        async def dashboard_performers() -> HTMLResponse:
            return HTMLResponse(_page_html(_assistant_on))


def _register_dashboard_personas(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/personas", response_class=HTMLResponse)
        async def dashboard_personas() -> HTMLResponse:
            return HTMLResponse(_page_html(_assistant_on))


def _register_dashboard_history(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/history", response_class=HTMLResponse)
        async def dashboard_history() -> HTMLResponse:
            return HTMLResponse(_page_html(_assistant_on))


def _register_dashboard_symphonies(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/symphonies", response_class=HTMLResponse)
        async def dashboard_symphonies() -> HTMLResponse:
            """Display the symphonies list page (Task 8)."""
            return HTMLResponse(_page_html(_assistant_on))


def _register_dashboard_symphony_detail(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/symphonies/{name}", response_class=HTMLResponse)
        async def dashboard_symphony_detail(name: str) -> HTMLResponse:
            """Display the detail page for a specific symphony (Task 8)."""
            return HTMLResponse(_page_html(_assistant_on))


def _register_dashboard_admin_config(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/admin/config", response_class=HTMLResponse)
        async def dashboard_admin_config() -> HTMLResponse:
            """Display the admin configuration page (Task 11)."""
            return HTMLResponse(_page_html(_assistant_on))


def _register_dashboard_config(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/config", response_class=HTMLResponse)
        async def dashboard_config() -> HTMLResponse:
            """Display the full live-config view (spec 081-config-ui)."""
            return HTMLResponse(_page_html(_assistant_on))


def register(app: FastAPI, ctx: DashboardContext) -> None:
    _register_activity_stream_script(app, ctx)
    _register_dashboard_static_script(app, ctx)
    _register_dashboard(app, ctx)
    _register_dashboard_performers(app, ctx)
    _register_dashboard_personas(app, ctx)
    _register_dashboard_history(app, ctx)
    _register_dashboard_symphonies(app, ctx)
    _register_dashboard_symphony_detail(app, ctx)
    _register_dashboard_admin_config(app, ctx)
    _register_dashboard_config(app, ctx)

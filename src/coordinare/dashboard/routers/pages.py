"""Dashboard page and static-asset routes (436)."""
from __future__ import annotations

import asyncio  # noqa: F401
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from coordinare import vocabulary
from coordinare.dashboard.assets import (
        _ACTIVITY_STREAM_JS,
        _DASHBOARD_BRAND_ASSETS,
        _DASHBOARD_BRAND_ETAGS,
        _DASHBOARD_JS_SOURCES,
        _DASHBOARD_SHELL_CSS,
        _page_html,
)
from coordinare.lib.path_segments import sanitize_segment

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
        async def dashboard_static_script(filename: str, request: Request) -> Response:
            """349: content-addressed dashboard JS.

            The ``?v=`` query in the page is the sha256 of the served bytes, so
            this can be cached as immutable: a redeploy that changes a file
            changes the URL and no browser renders a stale script.
            """
            if filename == "shell.css":
                return Response(_DASHBOARD_SHELL_CSS, media_type="text/css", headers={"Cache-Control": "no-cache"})
            brand = _DASHBOARD_BRAND_ASSETS.get(filename)
            if brand is not None:
                content, media_type = brand
                etag = _DASHBOARD_BRAND_ETAGS[filename]
                headers = {"Cache-Control": "public, no-cache", "ETag": etag}
                validators = {
                    value.strip().removeprefix("W/")
                    for value in request.headers.get("if-none-match", "").split(",")
                }
                if etag in validators or "*" in validators:
                    return Response(status_code=304, headers=headers)
                return Response(content, media_type=media_type, headers=headers)
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


def _register_dashboard_run_detail(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/performers/{role}", response_class=HTMLResponse)
        async def dashboard_run_detail(role: str) -> HTMLResponse:
            """146: the run detail for one role. The client checks the role against the live roles."""
            return HTMLResponse(_page_html(_assistant_on))


def _register_dashboard_history(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/history", response_class=HTMLResponse)
        async def dashboard_history() -> HTMLResponse:
            return HTMLResponse(_page_html(_assistant_on))


def _register_dashboard_glossary(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/glossary", response_class=HTMLResponse)
        async def dashboard_glossary() -> HTMLResponse:
            return HTMLResponse(_page_html(_assistant_on))

        @app.get("/api/vocabulary")
        async def dashboard_vocabulary() -> JSONResponse:
            """136: the glossary every display label comes from."""
            return JSONResponse(vocabulary.payload())


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


def card_address_valid(symphony: str, card: str) -> bool:
    """233: a card page address names two segments in the writer's sanitized form, as the run routes do."""
    return all(bool(value) and sanitize_segment(value) == value for value in (symphony, card))


def _register_dashboard_card_page(app: FastAPI, ctx: DashboardContext) -> None:
        _assistant_on = ctx.assistant_on

        @app.get("/cards/{symphony}/{card}", response_class=HTMLResponse)
        async def dashboard_card(symphony: str, card: str) -> HTMLResponse:
            """233: the card page. The client reads the timeline. An address the writer could not have made is a 404, and the shell still loads and says so."""
            status = 200 if card_address_valid(symphony, card) else 404
            return HTMLResponse(_page_html(_assistant_on), status_code=status)


def _register_dashboard_admin_config(app: FastAPI, ctx: DashboardContext) -> None:

        @app.get("/admin/config")
        async def dashboard_admin_config(request: Request) -> RedirectResponse:
            """143: the Global Config page is now the General tab of Settings.

            The query string is kept. The fragment is never sent to the server, so
            the redirect names the tab itself.
            """
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(url=f"/config{query}#global", status_code=301)


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
    _register_dashboard_run_detail(app, ctx)
    _register_dashboard_personas(app, ctx)
    _register_dashboard_history(app, ctx)
    _register_dashboard_glossary(app, ctx)
    _register_dashboard_symphonies(app, ctx)
    _register_dashboard_symphony_detail(app, ctx)
    _register_dashboard_card_page(app, ctx)
    _register_dashboard_admin_config(app, ctx)
    _register_dashboard_config(app, ctx)

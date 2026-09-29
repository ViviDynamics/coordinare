"""FastAPI app factory for the dashboard (436)."""
from __future__ import annotations

import socket
import sys
import time
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI, Request

from coordinare.dashboard.routers.oidc import register_oidc_routes
from coordinare.localhost_guard import (
    PermittedOrigins,
    build_permitted,
    install_localhost_guard,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.responses import Response
    from pydantic import SecretStr
    from starlette.middleware.base import RequestResponseEndpoint

    from coordinare.daemon import CoordinareDaemon
    from coordinare.dashboard.store import DashboardStore
    from coordinare.dashboard_oidc import OidcFlow
    from coordinare.metrics import CoordinareMetrics
    from coordinare.observability import HealthRegistry

from coordinare.dashboard import routers
from coordinare.dashboard.context import DashboardContext
from coordinare.dashboard.guards import _config_assistant_enabled

_log = structlog.get_logger(__name__)


def create_dashboard_app(
    store: DashboardStore,
    daemon: CoordinareDaemon,
    metrics: CoordinareMetrics,
    health: HealthRegistry,
    config_path: Path | None = None,
    *,
    permitted_origins: PermittedOrigins | None = None,
    guard_exempt_paths: frozenset[str] | None = None,
    auth_token: SecretStr | None = None,
    signed_webhook_paths: frozenset[str] = frozenset(),
    oidc: OidcFlow | None = None,
) -> FastAPI:
    """Create the dashboard FastAPI application.

    Endpoints:
        GET /        — serves the dashboard HTML page
        GET /events  — SSE stream of state_update events
        GET /api/personas          — list all role personas (018)
        PUT /api/personas/{role}   — update persona for a role (018)
        DELETE /api/personas/{role} — reset persona to defaults (018)

    ``permitted_origins`` configures the localhost guard (spec 144). When omitted
    it defaults to loopback on the default dashboard port, which is the safe
    posture; production passes a set derived from the live configuration so the
    operator's bind address, port, and any trusted proxy hostname are honoured.
    """
    app = FastAPI(title="coordinare-dashboard")
    if auth_token is not None or oidc is not None:
        from coordinare.dashboard_auth import DashboardAuthentication
        app.add_middleware(
            DashboardAuthentication, token=auth_token, oidc=oidc,
            signed_webhook_paths=signed_webhook_paths,
        )
        if oidc is not None:
            register_oidc_routes(app, oidc)

    # Spec 144 (#198). Installed BEFORE the request logger deliberately.
    # FastAPI middleware is outermost-last, so the logger added below wraps this
    # guard: every request including a rejected one still appears in the request
    # log, while the guard refuses before any route handler runs.
    install_localhost_guard(
        app,
        permitted_origins
        or build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
        exempt_paths=guard_exempt_paths,
    )

    @app.middleware("http")
    async def _log_requests(
        request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        t0 = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        is_sse = request.url.path == "/events"
        log_kwargs: dict[str, Any] = {
            "method": request.method,
            "path": str(request.url.path),
            "status_code": response.status_code,
            "response_time_ms": elapsed_ms,
        }
        if is_sse:
            log_kwargs["streaming"] = True
        _log.info("http_request", **log_kwargs)
        return response

    ctx = DashboardContext(
        daemon=daemon,
        store=store,
        metrics=metrics,
        health=health,
        config_path=config_path,
        assistant_on=_config_assistant_enabled(daemon),
    )
    for area in (
        routers.pages,
        routers.state,
        routers.controls,
        routers.symphonies,
        routers.config,
        routers.personas,
        routers.assistant,
    ):
        area.register(app, ctx)
    return app


def check_port_available(host: str, port: int, *, label: str = "server") -> None:
    """Probe that a TCP port is available before starting a uvicorn server.

    Prints a short human-readable error and calls sys.exit(1) if the port is
    already in use — avoids the full uvicorn/asyncio traceback that would
    otherwise appear.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            _log.error(
                "dashboard_port_conflict",
                label=label,
                host=host,
                port=port,
                hint=f"Address {host}:{port} is already in use. "
                     "Stop the process holding that port and try again.",
            )
            sys.exit(1)

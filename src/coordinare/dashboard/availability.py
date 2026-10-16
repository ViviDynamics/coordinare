from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.responses import Response

STARTUP_RETRY_SECONDS = 2
SERVICE_UNAVAILABLE = 503


def daemon_ready(daemon: Any) -> bool:
    return bool(daemon.running) and daemon.state.get("startup_complete", False) is True


def install_availability(app: FastAPI, daemon: Any) -> None:
    """Install inside authentication so startup never bypasses credentials."""
    @app.get("/dashboard/ready")
    async def dashboard_ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.middleware("http")
    async def starting(request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        needs_state = path == "/events" or path.startswith("/api/")
        if needs_state and path != "/api/account" and not daemon_ready(daemon):
            state = daemon.state
            status = ("stopping" if state.get("stopping", False) else
                      "unavailable" if state.get("startup_complete", False) else "starting")
            headers = {"Cache-Control": "no-store"}
            if status in {"starting", "stopping"}:
                headers["Retry-After"] = str(STARTUP_RETRY_SECONDS)
            return JSONResponse(
                {"status": status, "message": f"Coordinare is {status}", "daemon_ready": False},
                status_code=SERVICE_UNAVAILABLE,
                headers=headers,
            )
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

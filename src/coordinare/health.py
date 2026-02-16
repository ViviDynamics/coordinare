from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Response, status

from coordinare.metrics import METRICS

if TYPE_CHECKING:
    from coordinare.daemon import CoordinareDaemon


def create_health_app(daemon: CoordinareDaemon) -> FastAPI:
    app = FastAPI(title="coordinare-health")

    @app.get("/health")
    async def health(response: Response) -> dict[str, Any]:
        daemon_running = daemon.running
        core_connected = daemon_running
        health_status = "healthy" if daemon_running else "unhealthy"

        if not core_connected:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        state = daemon.state
        current_card = state.get("current_card")
        card_payload: dict[str, Any] | None = None
        if isinstance(current_card, dict):
            card_payload = {
                "id": current_card.get("id"),
                "issue_number": current_card.get("issue_number"),
                "title": current_card.get("title"),
                "status": current_card.get("status"),
            }

        return {
            "status": health_status,
            "uptime_seconds": 0,
            "current_card": card_payload,
            "services": {
                "github": {
                    "status": "connected" if daemon_running else "disconnected",
                    "last_poll_at": state.get("last_poll_at"),
                },
                "agent_ssh": {"status": "connected" if daemon_running else "disconnected"},
                "smtp": {"status": "connected" if daemon_running else "disconnected"},
                "slack": {"status": "connected" if daemon_running else "disconnected"},
            },
            "timestamp": datetime.now(UTC),
        }

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, Any]:
        if daemon.running:
            return {"ready": True}
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ready": False, "reason": "daemon not running"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=METRICS.render(), media_type="text/plain; version=0.0.4")

    return app

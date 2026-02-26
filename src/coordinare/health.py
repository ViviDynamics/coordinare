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

        # Infer GitHub connectivity from last_poll_at recency (within 2 poll cycles = 60s).
        # Other services have no observable connectivity signal; report "unknown" while
        # the daemon is running rather than falsely claiming "connected".
        last_poll_at = state.get("last_poll_at")
        if not daemon_running:
            github_status = "disconnected"
        elif isinstance(last_poll_at, datetime):
            age_seconds = (datetime.now(UTC) - last_poll_at).total_seconds()
            github_status = "connected" if age_seconds < 60 else "degraded"
        else:
            github_status = "unknown"

        passive_service_status = "unknown" if daemon_running else "disconnected"

        # T027: Expose persisted state phase and snapshot_at from StateStore
        snapshot = None
        if daemon.state_store is not None:
            snapshot = daemon.state_store.last_snapshot
        phase = snapshot.phase if snapshot else None
        snapshot_at = snapshot.snapshot_at.isoformat() if snapshot else None

        return {
            "status": health_status,
            "phase": phase,
            "snapshot_at": snapshot_at,
            "uptime_seconds": 0,
            "current_card": card_payload,
            "services": {
                "github": {
                    "status": github_status,
                    "last_poll_at": last_poll_at,
                },
                "agent_ssh": {"status": passive_service_status},
                "smtp": {"status": passive_service_status},
                "slack": {"status": passive_service_status},
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

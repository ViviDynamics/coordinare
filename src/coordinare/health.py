from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Response, status

from coordinare.metrics import METRICS
from coordinare.observability import HEALTH, HealthStatus
from coordinare.resilience import CircuitBreaker, CircuitState

if TYPE_CHECKING:
    from coordinare.daemon import CoordinareDaemon
    from coordinare.observability import HealthRegistry

# Core service circuits — if any of these are open, /health status is "degraded"
_CORE_CIRCUITS = {"github", "anthropic", "agent"}


def create_health_app(
    daemon: CoordinareDaemon,
    *,
    circuit_breakers: dict[str, CircuitBreaker] | None = None,
    health_registry: HealthRegistry | None = None,
) -> FastAPI:
    app = FastAPI(title="coordinare-health")
    cbs = circuit_breakers or {}
    registry = health_registry or HEALTH
    _start_time = monotonic()

    @app.get("/health")
    async def health(response: Response) -> dict[str, Any]:
        daemon_running = daemon.running

        # Build circuit breaker status dict
        cb_status: dict[str, dict[str, Any]] = {}
        any_core_open = False
        for name, cb in cbs.items():
            opened_at_iso = cb.opened_at.isoformat() if cb.opened_at else None
            cb_status[name] = {
                "state": cb.state.value,
                "opened_at": opened_at_iso,
                "failure_count": len(cb._failure_times),
            }
            if name in _CORE_CIRCUITS and cb.state != CircuitState.CLOSED:
                any_core_open = True

        # Derive status from daemon state and circuit breakers
        if not daemon_running:
            health_status = "unhealthy"
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        elif any_core_open:
            health_status = "degraded"
        else:
            health_status = "ok"

        # Expose persisted state phase and snapshot_at from StateStore
        snapshot = None
        if daemon.state_store is not None:
            snapshot = daemon.state_store.last_snapshot
        phase = snapshot.phase if snapshot else "idle"
        snapshot_at = snapshot.snapshot_at.isoformat() if snapshot else None

        # 076 — surface the daemon's start-time so an operator (and the
        # reconciliation pass running inside the SAME process) can correlate
        # `coordinare.daemon_started_at` Docker labels back to which daemon
        # process launched a given performer container.
        from coordinare.daemon import get_daemon_started_at

        return {
            "status": health_status,
            "phase": phase,
            "snapshot_at": snapshot_at,
            "circuit_breakers": cb_status,
            "uptime_seconds": round(monotonic() - _start_time, 1),
            "daemon_started_at": get_daemon_started_at(),
        }

    @app.get("/live")
    async def live() -> dict[str, str]:
        """Liveness probe — always 200 while process is running."""
        return {"status": "alive"}

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, Any]:
        """Readiness probe — 200 if all required subsystems are healthy, 503 otherwise."""
        report = registry.snapshot()

        subsystems = [
            {
                "name": probe.subsystem_name,
                "status": probe.status.value,
                "required": probe.is_required,
                "checked_at": probe.checked_at.isoformat(),
                "details": probe.details,
            }
            for probe in report.probes
        ]

        if report.overall_status != HealthStatus.healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return {
            "status": "ready" if report.overall_status == HealthStatus.healthy else "degraded",
            "subsystems": subsystems,
            "response_time_ms": report.response_time_ms,
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=METRICS.render(), media_type="text/plain; version=0.0.4")

    return app

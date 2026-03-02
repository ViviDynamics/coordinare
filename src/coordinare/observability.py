"""Observability primitives: cycle correlation context and health registry (009)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic

import structlog.contextvars

__all__ = [
    "HEALTH",
    "CycleContext",
    "HealthProbe",
    "HealthRegistry",
    "HealthReport",
    "HealthStatus",
    "bind_cycle_id",
    "clear_cycle_id",
]


# ---------------------------------------------------------------------------
# Cycle correlation context (US2)
# ---------------------------------------------------------------------------


@dataclass
class CycleContext:
    """Ephemeral correlation context bound to one poll cycle."""

    cycle_id: str
    started_at: datetime


def bind_cycle_id(cycle_id: str) -> None:
    """Bind cycle_id into the structlog context for the current async task."""
    structlog.contextvars.bind_contextvars(cycle_id=cycle_id)


def clear_cycle_id() -> None:
    """Remove the cycle_id context var for the current async task."""
    structlog.contextvars.unbind_contextvars("cycle_id")


# ---------------------------------------------------------------------------
# Health probe types (US3)
# ---------------------------------------------------------------------------


class HealthStatus(StrEnum):
    healthy = "healthy"
    degraded = "degraded"
    unavailable = "unavailable"


@dataclass
class HealthProbe:
    """Current health state of one subsystem."""

    subsystem_name: str
    status: HealthStatus
    is_required: bool
    checked_at: datetime
    details: str | None = None


@dataclass
class HealthReport:
    """Aggregated snapshot of all registered health probes."""

    overall_status: HealthStatus
    probes: list[HealthProbe] = field(default_factory=list)
    response_time_ms: float = 0.0


# ---------------------------------------------------------------------------
# Health registry (US3)
# ---------------------------------------------------------------------------


class HealthRegistry:
    """Per-subsystem health probe store for the single-threaded async event loop.

    Updated by the daemon poll cycle; read synchronously by /ready endpoint.
    Not thread-safe — designed for use within a single asyncio event loop.
    """

    def __init__(self, timeout_seconds: int = 2) -> None:
        self._probes: dict[str, HealthProbe] = {}
        self._timeout_seconds = timeout_seconds

    def configure(self, *, timeout_seconds: int) -> None:
        """Update the stale-detection timeout (call once at startup before any probes)."""
        self._timeout_seconds = timeout_seconds

    def register(self, name: str, *, required: bool = True) -> None:
        """Register a subsystem; initial status is unavailable."""
        self._probes[name] = HealthProbe(
            subsystem_name=name,
            status=HealthStatus.unavailable,
            is_required=required,
            checked_at=datetime.now(UTC),
        )

    def update(
        self,
        name: str,
        status: HealthStatus,
        *,
        details: str | None = None,
    ) -> None:
        """Record latest probe result for a registered subsystem."""
        probe = self._probes.get(name)
        if probe is None:
            return
        self._probes[name] = HealthProbe(
            subsystem_name=name,
            status=status,
            is_required=probe.is_required,
            checked_at=datetime.now(UTC),
            details=details,
        )

    def snapshot(self) -> HealthReport:
        """Return current HealthReport (synchronous, non-blocking)."""
        t0 = monotonic()
        now = datetime.now(UTC)
        probes: list[HealthProbe] = []
        any_required_unhealthy = False

        for probe in self._probes.values():
            age_seconds = (now - probe.checked_at).total_seconds()
            if age_seconds > self._timeout_seconds and probe.status == HealthStatus.healthy:
                # Stale — treat as degraded
                effective = HealthProbe(
                    subsystem_name=probe.subsystem_name,
                    status=HealthStatus.degraded,
                    is_required=probe.is_required,
                    checked_at=probe.checked_at,
                    details=(
                        f"Probe stale: last update {age_seconds:.0f}s ago "
                        f"(threshold: {self._timeout_seconds}s)"
                    ),
                )
            else:
                effective = probe

            probes.append(effective)
            if effective.is_required and effective.status != HealthStatus.healthy:
                any_required_unhealthy = True

        overall = HealthStatus.degraded if any_required_unhealthy else HealthStatus.healthy
        elapsed_ms = (monotonic() - t0) * 1000
        return HealthReport(
            overall_status=overall,
            probes=probes,
            response_time_ms=round(elapsed_ms, 3),
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

HEALTH: HealthRegistry = HealthRegistry()

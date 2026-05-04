"""Observability primitives: cycle correlation context and health registry (009)."""
from __future__ import annotations

import contextvars
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
    "bind_symphony",
    "clear_cycle_id",
    "clear_symphony",
    "get_current_symphony",
]


# ---------------------------------------------------------------------------
# Cycle correlation context (US2)
# ---------------------------------------------------------------------------

# Module-level ContextVar for symphony name propagation (spec 057)
# Updated by bind_symphony() and read by get_current_symphony()
_current_symphony: contextvars.ContextVar[str] = contextvars.ContextVar(
    "coordinare_symphony", default="__default__"
)


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


def bind_symphony(name: str) -> None:
    """Bind symphony name into the structlog context for the current async task."""
    structlog.contextvars.bind_contextvars(symphony=name)
    _current_symphony.set(name)


def clear_symphony() -> None:
    """Remove the symphony context var for the current async task."""
    structlog.contextvars.unbind_contextvars("symphony")
    _current_symphony.set("__default__")


def get_current_symphony() -> str:
    """Retrieve the bound symphony name from context, or '__default__' if not bound.

    Works in conjunction with bind_symphony() to propagate the symphony name to
    lower-level services that emit metrics but don't have direct access to the
    daemon state. Used for adding the symphony label to metric emissions.
    """
    return _current_symphony.get()


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

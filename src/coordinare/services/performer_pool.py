"""Performer pool with selection, fallover, exclusion, and recovery (spec 056, T036).

Maintains an in-memory registry of containerized performer endpoints (ephemeral
and persistent modes only). Tracks availability, capabilities, and consecutive
failures. Provides deterministic selection by registration order with fallover
to next candidate when the first is busy. Marks performers unavailable after
consecutive-failure threshold is reached; marks them available again on first
successful status check.

Subprocess performers are explicitly NOT tracked here (FR-024).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from prometheus_client import Counter

from coordinare.lifecycle import ROLE_TO_STAGE as _ROLE_TO_STAGE
from coordinare.models.performer_endpoint import (
    PerformerEndpointConfig,
    PerformerEndpointState,
)

if TYPE_CHECKING:
    from coordinare.services.http_performer_service import HTTPPerformerService

logger = structlog.get_logger(__name__)

# --- Prometheus counters (T041) ---
_performer_pool_status_polls_total = Counter(
    "performer_pool_status_polls_total",
    "Total status polls by result",
    labelnames=["result"],
)
_performer_pool_dispatch_total = Counter(
    "performer_pool_dispatch_total",
    "Total dispatch attempts by outcome",
    labelnames=["outcome"],
)
_performer_pool_excluded_total = Counter(
    "performer_pool_excluded_total",
    "Total performers excluded due to repeated failures",
)
_performer_pool_recovered_total = Counter(
    "performer_pool_recovered_total",
    "Total performers recovered after exclusion",
)


class PerformerPool:
    """In-memory registry and dispatcher for containerized performers.

    Tracks only ephemeral and persistent performers (FR-024).
    Subprocess performers bypass this pool entirely.
    """

    def __init__(self, failure_threshold: int = 5) -> None:
        """Initialize an empty pool.

        Args:
            failure_threshold: consecutive failed status checks before exclusion (FR-012).
        """
        self._registrations: dict[str, PerformerEndpointState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._services: dict[str, HTTPPerformerService | None] = {}
        self._failure_threshold = failure_threshold
        self._registration_order: list[str] = []

    def register(
        self,
        config: PerformerEndpointConfig,
        service: HTTPPerformerService | None,
    ) -> None:
        """Register a new performer (ephemeral or persistent only; FR-024).

        Args:
            config: the performer configuration.
            service: pre-bound HTTPPerformerService (None skips polling; provide a service for health-check/exclusion to work).

        Raises:
            ValueError: if mode is subprocess.
        """
        if config.mode == "subprocess":
            raise ValueError(
                "subprocess performers must not be registered with the pool (FR-024)",
            )
        if config.id in self._registrations:
            raise ValueError(
                f"performer {config.id!r} is already registered; unregister first",
            )

        normalized_roles = [_ROLE_TO_STAGE.get(r, r) for r in config.roles]

        state = PerformerEndpointState(
            id=config.id,
            mode=config.mode,  # type: ignore
            endpoint=config.endpoint,
            roles=normalized_roles,
            availability="unknown",
            capabilities=None,
            current_job_id=None,
            last_status_at=None,
            consecutive_failures=0,
            excluded_until_recovery=False,
        )

        self._registrations[config.id] = state
        self._locks[config.id] = asyncio.Lock()
        self._services[config.id] = service
        self._registration_order.append(config.id)

        logger.info(
            "performer_pool.registered",
            registration_id=config.id,
            mode=config.mode,
            endpoint=str(config.endpoint) if config.endpoint else None,
        )

    def unregister(self, id: str) -> None:
        """Remove a performer from the pool.

        Args:
            id: registration id to remove.
        """
        if id in self._registrations:
            del self._registrations[id]
            del self._locks[id]
            del self._services[id]
            self._registration_order.remove(id)
            logger.info("performer_pool.unregistered", registration_id=id)

    def select_for(
        self,
        role: str,
        backend: str,
        required_flags: set[str],
    ) -> PerformerEndpointState | None:
        """Select an idle, capable, non-excluded performer for dispatch.

        Returns the first idle, non-excluded candidate whose capabilities match
        the required backend and tool flags. Selection is deterministic by
        registration order and non-blocking (does not acquire locks).

        Args:
            role: the requested role (matched against performer.roles in config).
            backend: the required backend identifier.
            required_flags: set of required tool flags.

        Returns:
            The first matching PerformerEndpointState, or None if no candidate is available.
        """
        for perf_id in self._registration_order:
            state = self._registrations.get(perf_id)
            if state is None:  # pragma: no cover — order and registry always in sync
                continue

            # Skip if not idle
            if state.availability != "idle":
                continue

            # Skip if excluded until recovery
            if state.excluded_until_recovery:
                continue

            # Skip if role not served by this performer
            if role not in state.roles:
                continue

            # Check capability match
            if state.capabilities is None:
                # Unknown capabilities — skip (SC-006 prevention)
                continue

            # Backend must be present
            if backend not in state.capabilities.backends:
                continue

            # All required flags must be present
            if not required_flags.issubset(set(state.capabilities.tool_flags)):
                continue

            # Found a match
            return state

        return None

    async def poll_all(self) -> None:
        """Poll status for all performers (fans out GET /status requests).

        Updates availability, capabilities, consecutive_failures, and emits
        exclusion/recovery events. Called once per coordinare poll cycle.
        """
        tasks = []
        for perf_id in self._registration_order:
            tasks.append(self._poll_one(perf_id))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_one(self, perf_id: str) -> None:
        """Poll a single performer's status and update its state."""
        state = self._registrations.get(perf_id)
        service = self._services.get(perf_id)

        if state is None or service is None:
            return

        try:
            # Network call outside lock to avoid blocking concurrent polls.
            status = await service.check_health()
            availability = status.get("availability", "unknown")
        except Exception as e:
            error_msg = str(e)
        else:
            # check_health() returns error/unreachable as dict values, not exceptions.
            # Treat them as failures so the threshold/exclusion logic fires.
            health_status = status.get("status", "")
            error_msg = status.get("reason") if health_status in {"unreachable", "error"} else None

        if error_msg is not None:
            async with self._locks[perf_id]:
                state.consecutive_failures += 1
                state.last_status_at = datetime.now(UTC)

                logger.warning(
                    "performer_pool.poll_failed",
                    registration_id=perf_id,
                    error=error_msg,
                    consecutive_failures=state.consecutive_failures,
                    threshold=self._failure_threshold,
                )

                if (
                    state.consecutive_failures >= self._failure_threshold
                    and not state.excluded_until_recovery
                ):
                    state.availability = "unreachable"
                    state.excluded_until_recovery = True
                    _performer_pool_excluded_total.inc()
                    logger.warning(
                        "performer_pool.excluded",
                        registration_id=perf_id,
                        reason="status_failures",
                        consecutive_failures=state.consecutive_failures,
                    )
                    if state.current_job_id is not None:
                        state.current_job_id = None

            _performer_pool_status_polls_total.labels(result="fail").inc()
            return

        async with self._locks[perf_id]:
            state.consecutive_failures = 0
            state.last_status_at = datetime.now(UTC)

            if state.excluded_until_recovery:
                state.excluded_until_recovery = False
                # Use actual availability from the recovery response; fall back to idle
                # only if the performer reports an unrecognised value.
                state.availability = (
                    availability
                    if availability in {"idle", "busy", "starting", "draining"}
                    else "idle"
                )
                _performer_pool_recovered_total.inc()
                logger.info(
                    "performer_pool.recovered",
                    registration_id=perf_id,
                    reason="status_success",
                )
            else:
                if availability in {"idle", "busy", "starting", "draining"}:
                    state.availability = availability

            if "capabilities" in status:
                from coordinare.models.performer_endpoint import PerformerCapabilities

                state.capabilities = PerformerCapabilities.model_validate(
                    status["capabilities"],
                )

        _performer_pool_status_polls_total.labels(result="ok").inc()

    def mark_busy(self, id: str, job_id: str) -> None:
        """Mark a performer busy with a specific job.

        Args:
            id: registration id.
            job_id: the job being dispatched to this performer.
        """
        state = self._registrations.get(id)
        if state is not None:
            state.availability = "busy"
            state.current_job_id = job_id
            _performer_pool_dispatch_total.labels(outcome="accepted").inc()
            logger.info(
                "performer_pool.mark_busy",
                registration_id=id,
                job_id=job_id,
            )

    def mark_idle(self, id: str) -> None:
        """Mark a performer idle (job complete).

        Args:
            id: registration id.
        """
        state = self._registrations.get(id)
        if state is not None:
            state.availability = "idle"
            state.current_job_id = None
            logger.info(
                "performer_pool.mark_idle",
                registration_id=id,
            )

    def mark_unreachable(self, id: str, reason: str = "status_failures") -> None:
        """Mark a performer unreachable (failed status checks or readiness timeout).

        Args:
            id: registration id.
            reason: cause code (status_failures | readiness_timeout | capability_mismatch).
        """
        state = self._registrations.get(id)
        if state is not None:
            state.availability = "unreachable"
            state.excluded_until_recovery = True
            state.consecutive_failures = 0  # Reset counter on exclusion
            _performer_pool_excluded_total.inc()
            # Clear in-flight job if any
            state.current_job_id = None
            logger.warning(
                "performer_pool.excluded",
                registration_id=id,
                reason=reason,
            )

    def mark_recovered(self, id: str) -> None:
        """Mark a performer recovered from unreachable state.

        Args:
            id: registration id.
        """
        state = self._registrations.get(id)
        if state is not None:
            state.availability = "idle"
            state.excluded_until_recovery = False
            state.consecutive_failures = 0
            _performer_pool_recovered_total.inc()
            logger.info(
                "performer_pool.recovered",
                registration_id=id,
            )

    def get_state(self, id: str) -> PerformerEndpointState | None:
        """Get the current state of a performer (read-only).

        Args:
            id: registration id.

        Returns:
            The PerformerEndpointState, or None if not registered.
        """
        return self._registrations.get(id)

    def list_all(self) -> list[PerformerEndpointState]:
        """Return all registered performers in registration order.

        Returns:
            List of PerformerEndpointState objects.
        """
        return [self._registrations[id] for id in self._registration_order]


__all__ = [
    "PerformerPool",
]

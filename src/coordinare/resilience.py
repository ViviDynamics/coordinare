"""Circuit breaker, retry configuration, and resilient service wrappers.

Provides:
- CircuitState / CircuitOpenError / CircuitBreaker — three-state FSM
- RetryConfig — frozen dataclass wrapping stamina.retry() kwargs
- ResilientAgentService — Decorator applying retry + CB around AgentService
"""
from __future__ import annotations

import contextlib
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import stamina
import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from coordinare.graph.state import AgentServiceProtocol

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is blocked by an open circuit breaker."""

    def __init__(self, service_name: str) -> None:
        super().__init__(f"{service_name} circuit is open — call skipped")
        self.service_name = service_name


@dataclass
class CircuitBreaker:
    """Asyncio-safe three-state FSM circuit breaker.

    State transitions are synchronous (no awaits in FSM logic) so they
    are effectively atomic within a single event loop.
    """

    service_name: str
    failure_threshold: int
    recovery_window: float
    observation_window: float

    on_open_callback: Callable[[str, str], None] | None = None

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_times: deque[float] = field(default_factory=deque, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _opened_at_dt: datetime | None = field(default=None, init=False)
    _half_open_probe_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def opened_at(self) -> datetime | None:
        return self._opened_at_dt

    def _prune_old_failures(self) -> None:
        cutoff = time.monotonic() - self.observation_window
        while self._failure_times and self._failure_times[0] < cutoff:
            self._failure_times.popleft()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        previous = self._state
        self._state = new_state
        logger.warning(
            "circuit_breaker.state_changed",
            service=self.service_name,
            previous_state=previous.value,
            new_state=new_state.value,
            reason=reason,
        )
        # Update Prometheus gauge — import here to avoid circular imports
        from coordinare.metrics import METRICS

        for s in CircuitState:
            METRICS.circuit_breaker_state.labels(
                service=self.service_name, state=s.value
            ).set(1.0 if s == new_state else 0.0)

        if new_state == CircuitState.OPEN and self.on_open_callback is not None:
            with contextlib.suppress(Exception):
                self.on_open_callback(self.service_name, reason)

    def allow_request(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            if self._opened_at is not None and (
                time.monotonic() - self._opened_at >= self.recovery_window
            ):
                self._transition(CircuitState.HALF_OPEN, "recovery_window_elapsed")
                return True
            return False
        # HALF_OPEN
        if self._half_open_probe_in_flight:
            return False
        self._half_open_probe_in_flight = True
        return True

    def record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_probe_in_flight = False
            self._failure_times.clear()
            self._opened_at = None
            self._opened_at_dt = None
            self._transition(CircuitState.CLOSED, "probe_succeeded")
        elif self._state == CircuitState.CLOSED:
            pass  # no-op

    def record_failure(self) -> None:
        now = time.monotonic()
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_probe_in_flight = False
            self._opened_at = now
            self._opened_at_dt = datetime.now(UTC)
            self._transition(CircuitState.OPEN, "probe_failed")
            return
        # CLOSED
        self._failure_times.append(now)
        self._prune_old_failures()
        if len(self._failure_times) >= self.failure_threshold:
            self._opened_at = now
            self._opened_at_dt = datetime.now(UTC)
            self._transition(CircuitState.OPEN, "failure_threshold_exceeded")

    @asynccontextmanager
    async def guard(self) -> AsyncGenerator[None, None]:
        if not self.allow_request():
            raise CircuitOpenError(self.service_name)
        try:
            yield
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()


# ---------------------------------------------------------------------------
# Retry Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryConfig:
    """Per-service stamina.retry() parameters."""

    attempts: int
    wait_initial: float
    wait_max: float
    wait_jitter: float
    wait_exp_base: float = 2.0

    def to_stamina_kwargs(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "wait_initial": self.wait_initial,
            "wait_max": self.wait_max,
            "wait_jitter": self.wait_jitter,
            "wait_exp_base": self.wait_exp_base,
        }


# ---------------------------------------------------------------------------
# Resilient Agent Service (Decorator)
# ---------------------------------------------------------------------------


class ResilientAgentService:
    """Decorator implementing AgentServiceProtocol.

    Wraps a concrete AgentService with stamina retry + circuit breaker.
    check_health() is NOT retried (fixed 10s timeout per spec 004 FR-013).
    """

    def __init__(
        self,
        inner: AgentServiceProtocol,
        retry_config: RetryConfig,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        self._inner = inner
        self._retry_config = retry_config
        self._circuit_breaker = circuit_breaker

    async def dispatch_card(self, card_context: dict[str, Any]) -> dict[str, Any]:
        from coordinare.metrics import METRICS
        from coordinare.transport.base import TransportTimeoutError

        retry = stamina.retry(
            on=TransportTimeoutError,
            **self._retry_config.to_stamina_kwargs(),
        )

        @retry
        async def _retried() -> dict[str, Any]:
            return await self._inner.dispatch_card(card_context)

        try:
            async with self._circuit_breaker.guard():
                result = await _retried()
            METRICS.service_calls_total.labels(
                service="agent", action="dispatch_card", outcome="success",
            ).inc()
            return result
        except Exception:
            METRICS.service_calls_total.labels(
                service="agent", action="dispatch_card", outcome="failure",
            ).inc()
            raise

    async def check_health(self) -> dict[str, Any]:
        return await self._inner.check_health()

    async def check_status(self, session_id: str) -> dict[str, Any]:
        from coordinare.metrics import METRICS
        from coordinare.transport.base import TransportTimeoutError

        retry = stamina.retry(
            on=TransportTimeoutError,
            **self._retry_config.to_stamina_kwargs(),
        )

        @retry
        async def _retried() -> dict[str, Any]:
            return await self._inner.check_status(session_id)

        try:
            async with self._circuit_breaker.guard():
                result = await _retried()
            METRICS.service_calls_total.labels(
                service="agent", action="check_status", outcome="success",
            ).inc()
            return result
        except Exception:
            METRICS.service_calls_total.labels(
                service="agent", action="check_status", outcome="failure",
            ).inc()
            raise

    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]:
        from coordinare.metrics import METRICS
        from coordinare.transport.base import TransportTimeoutError

        retry = stamina.retry(
            on=TransportTimeoutError,
            **self._retry_config.to_stamina_kwargs(),
        )

        @retry
        async def _retried() -> dict[str, Any]:
            return await self._inner.relay_feedback(review_payload)

        try:
            async with self._circuit_breaker.guard():
                result = await _retried()
            METRICS.service_calls_total.labels(
                service="agent", action="relay_feedback", outcome="success",
            ).inc()
            return result
        except Exception:
            METRICS.service_calls_total.labels(
                service="agent", action="relay_feedback", outcome="failure",
            ).inc()
            raise

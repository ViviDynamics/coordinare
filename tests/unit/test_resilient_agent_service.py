"""Unit tests for ResilientAgentService (T022).

Validates retry semantics, circuit-breaker guard propagation,
and the check_health bypass behaviour.

NOTE: The project-wide conftest disables stamina retries
(``stamina.set_active(False)``).  With stamina inactive the ``@retry``
decorator is a passthrough — the inner callable executes exactly once
regardless of the ``on=`` filter.  Tests that need real retry behaviour
re-enable stamina for the duration of the test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import stamina

from coordinare.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ResilientAgentService,
    RetryConfig,
)
from coordinare.transport.base import TransportError, TransportTimeoutError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _InnerAgent:
    """Minimal stub satisfying ``AgentServiceProtocol``."""

    def __init__(self) -> None:
        self.dispatch_card = AsyncMock(return_value={"ok": True})
        self.check_health = AsyncMock(return_value={"healthy": True})
        self.check_status = AsyncMock(return_value={"status": "running"})
        self.relay_feedback = AsyncMock(return_value={"relayed": True})


def _make_cb(
    *,
    failure_threshold: int = 3,
    recovery_window: float = 60.0,
    observation_window: float = 120.0,
) -> CircuitBreaker:
    return CircuitBreaker(
        service_name="test-agent",
        failure_threshold=failure_threshold,
        recovery_window=recovery_window,
        observation_window=observation_window,
    )


def _make_retry(*, attempts: int = 2) -> RetryConfig:
    return RetryConfig(
        attempts=attempts,
        wait_initial=0.0,
        wait_max=0.0,
        wait_jitter=0.0,
    )


# ---------------------------------------------------------------------------
# (a) TransportTimeoutError triggers stamina retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_retries_on_transport_timeout() -> None:
    """With stamina active and attempts=2, a transient TransportTimeoutError
    on the first call is retried and the second (successful) call wins."""

    inner = _InnerAgent()
    inner.dispatch_card.side_effect = [
        TransportTimeoutError(timeout=30),
        {"ok": True},
    ]

    cb = _make_cb()
    retry_cfg = _make_retry(attempts=2)
    svc = ResilientAgentService(inner, retry_cfg, cb)

    stamina.set_active(True)
    try:
        result = await svc.dispatch_card({"card": "data"})
    finally:
        stamina.set_active(False)

    assert result == {"ok": True}
    assert inner.dispatch_card.call_count == 2


# ---------------------------------------------------------------------------
# (b) Permanent TransportError propagates immediately (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_propagates_transport_error_immediately() -> None:
    """A generic ``TransportError`` (non-timeout) is NOT in the retry ``on=``
    filter, so it propagates on the first occurrence even with stamina active."""

    inner = _InnerAgent()
    inner.dispatch_card.side_effect = TransportError("connection refused")

    cb = _make_cb()
    retry_cfg = _make_retry(attempts=3)
    svc = ResilientAgentService(inner, retry_cfg, cb)

    stamina.set_active(True)
    try:
        with pytest.raises(TransportError, match="connection refused"):
            await svc.dispatch_card({"card": "data"})
    finally:
        stamina.set_active(False)

    # Called exactly once — no retry for non-timeout errors.
    assert inner.dispatch_card.call_count == 1


# ---------------------------------------------------------------------------
# (c) CircuitOpenError propagates when circuit is open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_raises_circuit_open_error() -> None:
    """When the circuit breaker is already open, ``dispatch_card`` raises
    ``CircuitOpenError`` without calling the inner service at all."""

    inner = _InnerAgent()
    cb = _make_cb(failure_threshold=1)
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    # Force the CB into OPEN state by recording enough failures.
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await svc.dispatch_card({"card": "data"})

    inner.dispatch_card.assert_not_called()


# ---------------------------------------------------------------------------
# (d) check_health bypasses circuit breaker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_health_bypasses_circuit_breaker() -> None:
    """``check_health()`` delegates directly to the inner service, ignoring
    the circuit breaker state entirely."""

    inner = _InnerAgent()
    cb = _make_cb(failure_threshold=1)
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    # Force the CB open.
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    result = await svc.check_health()

    assert result == {"healthy": True}
    inner.check_health.assert_awaited_once()


# ---------------------------------------------------------------------------
# (e) Successful dispatch_card records success on the CB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_records_success_on_circuit_breaker() -> None:
    """A successful ``dispatch_card`` call invokes ``record_success()``
    on the circuit breaker (via the ``guard()`` context manager)."""

    inner = _InnerAgent()
    cb = _make_cb()
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    with patch.object(cb, "record_success", wraps=cb.record_success) as spy:
        await svc.dispatch_card({"card": "data"})
        spy.assert_called_once()


# ---------------------------------------------------------------------------
# (f) Successful response is NOT retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_does_not_retry_on_success() -> None:
    """When ``dispatch_card`` returns a normal response the inner service
    is called exactly once — no spurious retries."""

    inner = _InnerAgent()
    inner.dispatch_card.return_value = {"ok": True}

    cb = _make_cb()
    retry_cfg = _make_retry(attempts=3)
    svc = ResilientAgentService(inner, retry_cfg, cb)

    stamina.set_active(True)
    try:
        result = await svc.dispatch_card({"card": "data"})
    finally:
        stamina.set_active(False)

    assert result == {"ok": True}
    assert inner.dispatch_card.call_count == 1


# ---------------------------------------------------------------------------
# Additional: check_status and relay_feedback exercise same retry + CB path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_status_retries_on_transport_timeout() -> None:
    """``check_status`` retries on TransportTimeoutError just like
    ``dispatch_card``."""

    inner = _InnerAgent()
    inner.check_status.side_effect = [
        TransportTimeoutError(timeout=10),
        {"status": "running"},
    ]

    cb = _make_cb()
    retry_cfg = _make_retry(attempts=2)
    svc = ResilientAgentService(inner, retry_cfg, cb)

    stamina.set_active(True)
    try:
        result = await svc.check_status("sess-1")
    finally:
        stamina.set_active(False)

    assert result == {"status": "running"}
    assert inner.check_status.call_count == 2


@pytest.mark.asyncio
async def test_relay_feedback_raises_circuit_open_error() -> None:
    """``relay_feedback`` is also guarded by the circuit breaker."""

    inner = _InnerAgent()
    cb = _make_cb(failure_threshold=1)
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await svc.relay_feedback({"review": "lgtm"})

    inner.relay_feedback.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_card_records_failure_on_circuit_breaker() -> None:
    """When the inner call raises, the circuit breaker records a failure."""

    inner = _InnerAgent()
    inner.dispatch_card.side_effect = TransportError("boom")

    cb = _make_cb()
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    with patch.object(cb, "record_failure", wraps=cb.record_failure) as spy:
        with pytest.raises(TransportError):
            await svc.dispatch_card({"card": "data"})
        spy.assert_called_once()

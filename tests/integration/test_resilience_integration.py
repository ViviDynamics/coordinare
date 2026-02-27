"""T027/T028: Integration tests for resilience layer.

T027: GitHub circuit breaker integration — failures trip circuit, recovery closes it.
T028: Notification failures do not block card transitions.
T033: circuit_breaker_trip callback fires on OPEN transition.
"""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.notify import notify
from coordinare.graph.state import initial_state
from coordinare.resilience import CircuitBreaker, CircuitOpenError, CircuitState
from tests.utils.fake_notification import FakeNotificationService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cb(
    name: str = "test",
    threshold: int = 3,
    recovery: float = 120.0,
    observation: float = 300.0,
) -> CircuitBreaker:
    return CircuitBreaker(
        service_name=name,
        failure_threshold=threshold,
        recovery_window=recovery,
        observation_window=observation,
    )


# ---------------------------------------------------------------------------
# T027: Circuit breaker integration — open / half-open / close cycle
# ---------------------------------------------------------------------------


def test_circuit_opens_after_threshold_failures() -> None:
    """After failure_threshold exhausted retry budgets, circuit transitions to OPEN."""
    cb = _make_cb(threshold=3)
    assert cb.state == CircuitState.CLOSED

    for _ in range(3):
        cb.record_failure()

    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_open_circuit_raises_circuit_open_error() -> None:
    """Subsequent calls to guard() raise CircuitOpenError when circuit is OPEN."""
    cb = _make_cb(threshold=1)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError, match="test"):
        async with cb.guard():
            pass  # Should never reach here


@pytest.mark.asyncio
async def test_circuit_closes_after_successful_probe() -> None:
    """After recovery_window, probe success closes the circuit."""
    cb = _make_cb(threshold=1, recovery=0.0)  # instant recovery
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # recovery_window=0.0 means allow_request() transitions to HALF_OPEN immediately
    assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# T028: Notification failures do not block card transitions
# ---------------------------------------------------------------------------


class _FailingNotification:
    async def dispatch(self, event: object) -> None:
        raise ConnectionError("notification channel unreachable")


@pytest.mark.asyncio
async def test_notify_continues_after_dispatch_failure() -> None:
    """Notification dispatch failure does not prevent node from returning."""
    state = initial_state()
    state.update({
        "current_card": {
            "title": "Card", "status": "IN_PROGRESS",
            "previous_status": "TODO", "description": "Task",
        },
        "notification_service": _FailingNotification(),
    })

    # Should NOT raise
    result = await notify(state)
    assert result is not None


@pytest.mark.asyncio
async def test_notify_completes_with_working_service() -> None:
    """Normal dispatch completes without raising."""
    fake = FakeNotificationService()
    state = initial_state()
    state.update({
        "current_card": {
            "title": "Card", "status": "IN_PROGRESS",
            "previous_status": "TODO", "description": "Task",
        },
        "notification_service": fake,
    })

    result = await notify(state)

    assert result is not None
    assert len(fake.dispatched) == 1


# ---------------------------------------------------------------------------
# T033: circuit_breaker_trip callback
# ---------------------------------------------------------------------------


def test_on_open_callback_fires_when_circuit_trips() -> None:
    """on_open_callback is called when circuit transitions to OPEN."""
    calls: list[tuple[str, str]] = []

    cb = _make_cb(threshold=1)
    cb.on_open_callback = lambda service, reason: calls.append((service, reason))

    cb.record_failure()

    assert cb.state == CircuitState.OPEN
    assert len(calls) == 1
    assert calls[0][0] == "test"
    assert calls[0][1] == "failure_threshold_exceeded"


def test_on_open_callback_not_called_when_closing() -> None:
    """on_open_callback is NOT called when circuit closes (only on OPEN)."""
    calls: list[tuple[str, str]] = []

    cb = _make_cb(threshold=1, recovery=0.0)
    cb.on_open_callback = lambda service, reason: calls.append((service, reason))

    cb.record_failure()  # → OPEN (callback fires once)
    cb.allow_request()   # → HALF_OPEN
    cb.record_success()  # → CLOSED (callback should NOT fire again)

    assert len(calls) == 1  # Only the OPEN transition


def test_on_open_callback_fires_on_probe_failure() -> None:
    """on_open_callback fires when HALF_OPEN probe fails → re-opens."""
    calls: list[tuple[str, str]] = []

    cb = _make_cb(threshold=1, recovery=0.0)
    cb.on_open_callback = lambda service, reason: calls.append((service, reason))

    cb.record_failure()  # → OPEN (first call)
    cb.allow_request()   # → HALF_OPEN
    cb.record_failure()  # → OPEN again (second call)

    assert len(calls) == 2
    assert calls[1][1] == "probe_failed"


def test_on_open_callback_exception_does_not_propagate() -> None:
    """A failing callback must not affect circuit state transitions."""
    def _bad_callback(service: str, reason: str) -> None:
        msg = "callback exploded"
        raise RuntimeError(msg)

    cb = _make_cb(threshold=1)
    cb.on_open_callback = _bad_callback

    # Should not raise
    cb.record_failure()

    assert cb.state == CircuitState.OPEN

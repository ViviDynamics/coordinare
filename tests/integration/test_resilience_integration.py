"""T027/T028: Integration tests for resilience layer.

T027: GitHub circuit breaker integration — failures trip circuit, recovery closes it.
T028: Notification failures do not block card transitions.
"""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.notify import notify
from coordinare.graph.state import initial_state
from coordinare.resilience import CircuitBreaker, CircuitOpenError, CircuitState

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


class _FailingEmail:
    async def send_notification(self, recipient, notification):
        raise ConnectionError("SMTP host unreachable")


class _FailingSlack:
    async def send_notification(self, notification):
        raise TimeoutError("Slack webhook timed out")


class _CountingEmail:
    def __init__(self) -> None:
        self.sent = 0

    async def send_notification(self, recipient, notification):
        self.sent += 1


class _CountingSlack:
    def __init__(self) -> None:
        self.sent = 0

    async def send_notification(self, notification):
        self.sent += 1


@pytest.mark.asyncio
async def test_notify_continues_after_email_failure() -> None:
    """Email failure does not prevent Slack delivery."""
    slack = _CountingSlack()
    state = initial_state()
    state.update({
        "current_card": {
            "title": "Card", "status": "IN_PROGRESS",
            "previous_status": "TODO", "description": "Task",
        },
        "email_service": _FailingEmail(),
        "slack_service": slack,
        "notification_email": "team@example.com",
    })

    result = await notify(state)

    # Slack should still have been called despite email failure
    assert slack.sent == 1
    # Node returns state without raising
    assert result is not None


@pytest.mark.asyncio
async def test_notify_continues_after_slack_failure() -> None:
    """Slack failure does not prevent email delivery."""
    email = _CountingEmail()
    state = initial_state()
    state.update({
        "current_card": {
            "title": "Card", "status": "IN_PROGRESS",
            "previous_status": "TODO", "description": "Task",
        },
        "email_service": email,
        "slack_service": _FailingSlack(),
        "notification_email": "team@example.com",
    })

    result = await notify(state)

    assert email.sent == 1
    assert result is not None


@pytest.mark.asyncio
async def test_notify_survives_both_channels_failing() -> None:
    """Both email and Slack failing does not raise — node returns normally."""
    state = initial_state()
    state.update({
        "current_card": {
            "title": "Card", "status": "IN_PROGRESS",
            "previous_status": "TODO", "description": "Task",
        },
        "email_service": _FailingEmail(),
        "slack_service": _FailingSlack(),
        "notification_email": "team@example.com",
    })

    # Should NOT raise
    result = await notify(state)
    assert result is not None

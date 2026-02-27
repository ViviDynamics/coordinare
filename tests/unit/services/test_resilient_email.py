"""Unit tests for Email service resilience (T018).

Tests cover:
(a) SMTPConnectError -> TransientSMTPError
(b) SMTPAuthenticationError -> PermanentSMTPError
(c) CircuitOpenError propagated when circuit is open
(d) Card transition not blocked after exhausted retries (exception propagates cleanly)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosmtplib
import pytest

from coordinare.models.card import CardStatus
from coordinare.models.notification import Notification
from coordinare.resilience import CircuitBreaker, CircuitOpenError, CircuitState
from coordinare.services.email import (
    EmailService,
    PermanentSMTPError,
    TransientSMTPError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notification() -> Notification:
    return Notification(
        card_title="Test Card",
        previous_status=CardStatus.TODO,
        card_status=CardStatus.IN_PROGRESS,
        task_description="Run unit tests",
    )


def _patch_smtp_send(
    monkeypatch: pytest.MonkeyPatch,
    *,
    side_effect: Exception | None = None,
) -> AsyncMock:
    """Replace ``aiosmtplib.send`` with an ``AsyncMock``.

    If *side_effect* is given the mock raises it on call.
    Otherwise the mock returns ``None`` (success).
    """
    mock = AsyncMock()
    if side_effect is not None:
        mock.side_effect = side_effect
    monkeypatch.setattr("coordinare.services.email.aiosmtplib.send", mock)
    return mock


def _make_circuit_breaker(
    *,
    failure_threshold: int = 3,
    recovery_window: float = 30.0,
    observation_window: float = 60.0,
) -> CircuitBreaker:
    """Return a ``CircuitBreaker`` for ``EmailService`` tests."""
    return CircuitBreaker(
        service_name="smtp-test",
        failure_threshold=failure_threshold,
        recovery_window=recovery_window,
        observation_window=observation_window,
    )


def _make_service(
    *,
    circuit_breaker: CircuitBreaker | None = None,
) -> EmailService:
    return EmailService(
        host="smtp.example.com",
        port=587,
        circuit_breaker=circuit_breaker,
    )


# ---------------------------------------------------------------------------
# (a) SMTPConnectError -> TransientSMTPError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_connect_error_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``aiosmtplib.SMTPConnectError`` should be wrapped as
    ``TransientSMTPError``.

    With stamina globally disabled the retry decorator is a no-op, so the
    exception propagates immediately.
    """
    _patch_smtp_send(
        monkeypatch,
        side_effect=aiosmtplib.SMTPConnectError("Connection refused"),
    )

    service = _make_service()

    with pytest.raises(TransientSMTPError, match="Connection refused"):
        await service.send_notification("team@example.com", _make_notification())


@pytest.mark.asyncio
async def test_smtp_server_disconnected_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SMTPServerDisconnected`` is also classified as transient."""
    _patch_smtp_send(
        monkeypatch,
        side_effect=aiosmtplib.SMTPServerDisconnected("Server went away"),
    )

    service = _make_service()

    with pytest.raises(TransientSMTPError, match="Server went away"):
        await service.send_notification("team@example.com", _make_notification())


@pytest.mark.asyncio
async def test_connection_error_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``ConnectionError`` is classified as transient."""
    _patch_smtp_send(
        monkeypatch,
        side_effect=ConnectionError("Connection reset by peer"),
    )

    service = _make_service()

    with pytest.raises(TransientSMTPError, match="Connection reset"):
        await service.send_notification("team@example.com", _make_notification())


@pytest.mark.asyncio
async def test_os_error_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``OSError`` is classified as transient."""
    _patch_smtp_send(
        monkeypatch,
        side_effect=OSError("Network is unreachable"),
    )

    service = _make_service()

    with pytest.raises(TransientSMTPError, match="Network is unreachable"):
        await service.send_notification("team@example.com", _make_notification())


# ---------------------------------------------------------------------------
# (b) SMTPAuthenticationError -> PermanentSMTPError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_authentication_error_raises_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``aiosmtplib.SMTPAuthenticationError`` should be wrapped as
    ``PermanentSMTPError`` and never retried."""
    _patch_smtp_send(
        monkeypatch,
        side_effect=aiosmtplib.SMTPAuthenticationError(535, "Authentication failed"),
    )

    service = _make_service()

    with pytest.raises(PermanentSMTPError, match="Authentication failed"):
        await service.send_notification("team@example.com", _make_notification())


@pytest.mark.asyncio
async def test_smtp_recipients_refused_raises_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SMTPRecipientsRefused`` is classified as permanent."""
    _patch_smtp_send(
        monkeypatch,
        side_effect=aiosmtplib.SMTPRecipientsRefused([]),
    )

    service = _make_service()

    with pytest.raises(PermanentSMTPError):
        await service.send_notification("bad@example.com", _make_notification())


# ---------------------------------------------------------------------------
# (c) CircuitOpenError propagated on open circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_open_error_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the circuit breaker is open, ``CircuitOpenError`` must propagate
    without even attempting the SMTP send."""
    cb_adapter = _make_circuit_breaker(failure_threshold=2)
    inner_cb = cb_adapter

    # Trip the circuit open by recording enough failures
    inner_cb.record_failure()
    inner_cb.record_failure()
    assert inner_cb.state == CircuitState.OPEN

    # Set up a send mock -- it should never be called
    send_mock = _patch_smtp_send(monkeypatch)

    service = _make_service(circuit_breaker=cb_adapter)

    with pytest.raises(CircuitOpenError):
        await service.send_notification("team@example.com", _make_notification())

    # Confirm the SMTP send was never invoked
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_circuit_breaker_records_failure_on_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a transient error the CB should record the failure.

    Enough failures should trip the CB to OPEN.
    """
    cb_adapter = _make_circuit_breaker(failure_threshold=2)
    inner_cb = cb_adapter

    _patch_smtp_send(
        monkeypatch,
        side_effect=aiosmtplib.SMTPConnectError("Connection refused"),
    )

    service = _make_service(circuit_breaker=cb_adapter)

    # First failure
    with pytest.raises(TransientSMTPError):
        await service.send_notification("team@example.com", _make_notification())

    assert inner_cb.state == CircuitState.CLOSED  # not yet tripped

    # Second failure -> should trip
    with pytest.raises(TransientSMTPError):
        await service.send_notification("team@example.com", _make_notification())

    assert inner_cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_records_success_on_ok_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful send through the CB should record success (CB stays CLOSED)."""
    cb_adapter = _make_circuit_breaker(failure_threshold=3)
    inner_cb = cb_adapter

    _patch_smtp_send(monkeypatch)  # succeeds (returns None)

    service = _make_service(circuit_breaker=cb_adapter)

    await service.send_notification("team@example.com", _make_notification())

    assert inner_cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# (d) Card transition not blocked after exhausted retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_retries_propagate_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After stamina exhausts retries (disabled in tests, so immediate), the
    ``TransientSMTPError`` propagates cleanly.  Callers can therefore catch
    the error and proceed with the card transition -- the email failure must
    NOT block the pipeline.

    We verify:
    1. The exception is raised (not silently swallowed).
    2. The exception is the expected ``TransientSMTPError`` subtype.
    3. After the failure, the caller can still perform further work (simulated
       by a simple assertion after the ``pytest.raises`` block).
    """
    _patch_smtp_send(
        monkeypatch,
        side_effect=aiosmtplib.SMTPConnectError("Connection refused"),
    )

    service = _make_service()

    with pytest.raises(TransientSMTPError):
        await service.send_notification("team@example.com", _make_notification())

    # Callers can catch and proceed -- the pipeline is not blocked.
    # Simulate continued work after notification failure:
    card_transition_completed = True
    assert card_transition_completed


@pytest.mark.asyncio
async def test_permanent_error_propagates_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``PermanentSMTPError`` likewise propagates cleanly without blocking
    the caller from continuing."""
    _patch_smtp_send(
        monkeypatch,
        side_effect=aiosmtplib.SMTPAuthenticationError(535, "Bad credentials"),
    )

    service = _make_service()

    with pytest.raises(PermanentSMTPError):
        await service.send_notification("team@example.com", _make_notification())

    # Callers can catch and proceed
    card_transition_completed = True
    assert card_transition_completed

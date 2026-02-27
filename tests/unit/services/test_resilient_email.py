"""Unit tests for Email service resilience (006-updated).

With spec 006, EmailService is a passive configuration holder.
Direct SMTP delivery is handled by EmailChannelSender in notification.py.
These tests verify the error class hierarchy and passive holder construction
with circuit breaker integration remain intact.
"""
from __future__ import annotations

from coordinare.resilience import CircuitBreaker
from coordinare.services.email import (
    EmailService,
    PermanentSMTPError,
    SMTPDeliveryError,
    TransientSMTPError,
)


def test_error_class_hierarchy() -> None:
    """Error classes maintain correct inheritance chain."""
    assert issubclass(TransientSMTPError, SMTPDeliveryError)
    assert issubclass(PermanentSMTPError, SMTPDeliveryError)
    assert issubclass(SMTPDeliveryError, RuntimeError)


def test_email_service_accepts_circuit_breaker() -> None:
    """EmailService constructor accepts circuit_breaker parameter."""
    cb = CircuitBreaker(
        service_name="smtp-test",
        failure_threshold=3,
        recovery_window=30.0,
        observation_window=60.0,
    )
    service = EmailService(
        host="smtp.example.com",
        port=587,
        circuit_breaker=cb,
    )
    assert service._circuit_breaker is cb


def test_email_service_default_retry_kwargs() -> None:
    """EmailService uses default retry kwargs when none provided."""
    service = EmailService(host="smtp.example.com", port=587)
    assert service._retry_kwargs["attempts"] == 1

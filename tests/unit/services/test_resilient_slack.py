"""Unit tests for Slack service resilience (006-updated).

With spec 006, SlackService is a passive configuration holder.
Direct webhook delivery is handled by SlackChannelSender in notification.py.
These tests verify the error class hierarchy and passive holder construction
with circuit breaker integration remain intact.
"""
from __future__ import annotations

from coordinare.resilience import CircuitBreaker
from coordinare.services.slack import (
    PermanentSlackError,
    SlackError,
    SlackService,
    TransientSlackError,
)


def test_error_class_hierarchy() -> None:
    """Error classes maintain correct inheritance chain."""
    assert issubclass(TransientSlackError, SlackError)
    assert issubclass(PermanentSlackError, SlackError)
    assert issubclass(SlackError, RuntimeError)


def test_slack_service_accepts_circuit_breaker() -> None:
    """SlackService constructor accepts circuit_breaker parameter."""
    cb = CircuitBreaker(
        service_name="slack-test",
        failure_threshold=3,
        recovery_window=30.0,
        observation_window=60.0,
    )
    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
        circuit_breaker=cb,
    )
    assert service._circuit_breaker is cb


def test_slack_service_default_retry_kwargs() -> None:
    """SlackService uses default retry kwargs when none provided."""
    service = SlackService("https://hooks.slack.com/services/x/y/z", "#eng")
    assert service._retry_kwargs["attempts"] == 1

"""Tests for notification.py uncovered constructors and factory (lines 50-55, 78-88, 203-204, 305-328)."""
from __future__ import annotations

import pytest

from coordinare.config import NotificationsConfig
from coordinare.metrics import METRICS
from coordinare.services.notification import (
    EmailChannelSender,
    SlackChannelSender,
    build_notification_service,
)

# ---------------------------------------------------------------------------
# SlackChannelSender — constructor and channel_name property (lines 50-55)
# ---------------------------------------------------------------------------


def test_slack_sender_stores_name() -> None:
    sender = SlackChannelSender(name="my-slack", webhook_url="https://hooks.slack.com/T1")
    assert sender._name == "my-slack"
    assert sender._webhook_url == "https://hooks.slack.com/T1"


def test_slack_sender_channel_name_property() -> None:
    sender = SlackChannelSender(name="alerts", webhook_url="https://hooks.slack.com/X")
    assert sender.channel_name == "alerts"


# ---------------------------------------------------------------------------
# EmailChannelSender — constructor and channel_name property (lines 78-88)
# ---------------------------------------------------------------------------


def test_email_sender_stores_fields() -> None:
    sender = EmailChannelSender(
        name="ops-email",
        smtp_host="mail.example.com",
        smtp_port=587,
        smtp_username="user@example.com",
        smtp_password="secret",
        sender="coordinare@example.com",
        recipient="ops@example.com",
    )
    assert sender._smtp_host == "mail.example.com"
    assert sender._smtp_port == 587
    assert sender._recipient == "ops@example.com"


def test_email_sender_channel_name_property() -> None:
    sender = EmailChannelSender(
        name="email-ops",
        smtp_host="smtp.example.com",
        smtp_port=25,
        smtp_username=None,
        smtp_password=None,
        sender="coordinare@example.com",
        recipient="team@example.com",
    )
    assert sender.channel_name == "email-ops"


def test_email_sender_optional_fields_can_be_none() -> None:
    sender = EmailChannelSender(
        name="simple",
        smtp_host="localhost",
        smtp_port=25,
        smtp_username=None,
        smtp_password=None,
        sender="noreply@example.com",
        recipient="admin@example.com",
    )
    assert sender._smtp_username is None
    assert sender._smtp_password is None


# ---------------------------------------------------------------------------
# build_notification_service — factory (lines 305-328)
# ---------------------------------------------------------------------------


def test_build_notification_service_with_no_channels() -> None:
    config = NotificationsConfig(channels=[], routing=[])
    svc = build_notification_service(config, METRICS)
    assert svc is not None


def test_build_notification_service_with_slack_channel() -> None:
    from pydantic import SecretStr

    from coordinare.config import ChannelConfig
    from coordinare.models.notification import ChannelType

    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="slack-alerts",
                type=ChannelType.slack,
                webhook_url=SecretStr("https://hooks.slack.com/T999"),
            )
        ],
        routing=[],
    )
    svc = build_notification_service(config, METRICS)
    assert "slack-alerts" in svc._senders


def test_build_notification_service_with_email_channel() -> None:
    from coordinare.config import ChannelConfig
    from coordinare.models.notification import ChannelType

    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="email-ops",
                type=ChannelType.email,
                smtp_host="smtp.example.com",
                smtp_recipient="ops@example.com",
            )
        ],
        routing=[],
    )
    svc = build_notification_service(config, METRICS)
    assert "email-ops" in svc._senders


# ---------------------------------------------------------------------------
# SlackChannelSender.send() — verifies POST with correct URL and JSON body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_sender_send_posts_correct_payload() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    sender = SlackChannelSender(name="test-slack", webhook_url="https://hooks.slack.com/T123")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    mock_async_context = MagicMock()
    mock_async_context.__aenter__ = AsyncMock(return_value=mock_client)
    mock_async_context.__aexit__ = AsyncMock(return_value=False)

    with patch("coordinare.services.notification.httpx.AsyncClient", return_value=mock_async_context):
        await sender.send("hello world")

    mock_client.post.assert_called_once_with(
        "https://hooks.slack.com/T123",
        json={"text": "hello world"},
    )
    mock_response.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_slack_sender_send_ignores_subject() -> None:
    """subject kwarg is accepted but not forwarded to Slack (Slack uses text only)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    sender = SlackChannelSender(name="s", webhook_url="https://hooks.slack.com/X")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    mock_async_context = MagicMock()
    mock_async_context.__aenter__ = AsyncMock(return_value=mock_client)
    mock_async_context.__aexit__ = AsyncMock(return_value=False)

    with patch("coordinare.services.notification.httpx.AsyncClient", return_value=mock_async_context):
        await sender.send("msg", subject="My Subject")

    mock_client.post.assert_called_once_with(
        "https://hooks.slack.com/X",
        json={"text": "msg"},
    )


# ---------------------------------------------------------------------------
# EmailChannelSender.send() — verifies aiosmtplib.send args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_sender_send_calls_aiosmtplib_with_correct_args() -> None:
    from unittest.mock import AsyncMock, patch

    sender = EmailChannelSender(
        name="email-test",
        smtp_host="mail.example.com",
        smtp_port=587,
        smtp_username="user@example.com",
        smtp_password="s3cr3t",
        sender="coordinare@example.com",
        recipient="ops@example.com",
    )

    with patch("coordinare.services.notification.aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        await sender.send("Test body", subject="Test Subject")

    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
    assert kwargs["hostname"] == "mail.example.com"
    assert kwargs["port"] == 587
    assert kwargs["username"] == "user@example.com"
    assert kwargs["password"] == "s3cr3t"
    assert kwargs["start_tls"] is True
    assert kwargs["timeout"] == 10
    # verify the EmailMessage was built correctly
    msg_arg = mock_send.call_args[0][0]
    assert msg_arg["From"] == "coordinare@example.com"
    assert msg_arg["To"] == "ops@example.com"
    assert msg_arg["Subject"] == "Test Subject"


@pytest.mark.asyncio
async def test_email_sender_send_uses_default_subject() -> None:
    from unittest.mock import AsyncMock, patch

    sender = EmailChannelSender(
        name="email-test",
        smtp_host="localhost",
        smtp_port=25,
        smtp_username=None,
        smtp_password=None,
        sender="noreply@example.com",
        recipient="team@example.com",
    )

    with patch("coordinare.services.notification.aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        await sender.send("body without subject")

    msg_arg = mock_send.call_args[0][0]
    assert msg_arg["Subject"] == "[Coordinare] Notification"


# ---------------------------------------------------------------------------
# _dispatch_to_channel — missing channel warning (line 203)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_to_channel_missing_channel_logs_warning() -> None:
    """Calling _dispatch_to_channel with an unknown channel name returns early."""
    import structlog.testing

    from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity
    from coordinare.services.notification import NotificationService

    config = NotificationsConfig(channels=[], routing=[])
    svc = NotificationService(config, [], METRICS)

    event = NotificationEvent(
        event_type=EventType.card_blocked,
        severity=NotificationSeverity.warning,
        payload={"event_type": "card_blocked", "source": "test"},
        source="test",
    )

    # _dispatch_to_channel should return without raising for an unknown channel
    with structlog.testing.capture_logs() as logs:
        await svc._dispatch_to_channel(event, "nonexistent-channel")

    warning_logs = [entry for entry in logs if entry.get("log_level") == "warning"]
    assert any("nonexistent-channel" in str(entry) for entry in warning_logs)


# ---------------------------------------------------------------------------
# SlidingWindowRateLimiter — max_messages=0 always allows (line 122)
# ---------------------------------------------------------------------------


def test_sliding_window_rate_limiter_zero_max_always_allows() -> None:
    """max_messages=0 means unlimited — is_allowed() always returns True."""
    from coordinare.services.notification import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(max_messages=0, window_seconds=60.0)
    for _ in range(100):
        assert limiter.is_allowed() is True


def test_sliding_window_rate_limiter_evicts_stale_timestamps() -> None:
    """Line 122 (popleft): stale timestamps older than the window are evicted."""
    from collections import deque
    from datetime import UTC, datetime, timedelta

    from coordinare.services.notification import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(max_messages=2, window_seconds=60.0)
    # Manually inject a timestamp that is 120 seconds in the past (outside the window)
    stale_time = datetime.now(UTC) - timedelta(seconds=120)
    limiter._timestamps = deque([stale_time])

    # Calling is_allowed() should evict the stale timestamp and return True
    result = limiter.is_allowed()
    assert result is True
    assert len(limiter._timestamps) == 1  # only the fresh timestamp from this call

"""Tests for notification.py uncovered constructors and factory (lines 50-55, 78-88, 203-204, 305-328)."""
from __future__ import annotations

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

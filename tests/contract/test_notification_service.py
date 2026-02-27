"""Contract tests for NotificationService and ChannelSender protocols (T016).

Verify that NotificationService satisfies NotificationServiceProtocol,
and that channel senders satisfy ChannelSenderProtocol.
"""
from __future__ import annotations

import inspect

from coordinare.config import ChannelConfig, NotificationsConfig, RoutingEntry
from coordinare.metrics import CoordinareMetrics
from coordinare.models.notification import EventType
from coordinare.services.notification import (
    EmailChannelSender,
    NotificationService,
    SlackChannelSender,
)


def _make_notification_service() -> NotificationService:
    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="slack-ops",
                type="slack",
                webhook_url="https://hooks.slack.com/services/T/B/C",
                message_template="{event_type}: {summary}",
            ),
        ],
        routing=[
            RoutingEntry(event_type=EventType.card_transition, channels=["slack-ops"]),
        ],
    )
    sender = SlackChannelSender(name="slack-ops", webhook_url="https://hooks.slack.com/services/T/B/C")
    return NotificationService(config=config, senders=[sender], metrics=CoordinareMetrics())


def test_notification_service_has_dispatch_coroutine() -> None:
    svc = _make_notification_service()
    assert hasattr(svc, "dispatch")
    assert inspect.iscoroutinefunction(svc.dispatch)


def test_notification_service_has_history_property() -> None:
    svc = _make_notification_service()
    assert hasattr(svc, "history")
    history = svc.history
    assert hasattr(history, "query")
    assert hasattr(history, "append")


def test_slack_channel_sender_satisfies_protocol() -> None:
    sender = SlackChannelSender(name="slack-ops", webhook_url="https://hooks.slack.com/services/T/B/C")
    assert hasattr(sender, "channel_name")
    assert sender.channel_name == "slack-ops"
    assert hasattr(sender, "send")
    assert inspect.iscoroutinefunction(sender.send)


def test_email_channel_sender_satisfies_protocol() -> None:
    sender = EmailChannelSender(
        name="email-team",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        sender="coordinare@example.com",
        recipient="team@example.com",
    )
    assert hasattr(sender, "channel_name")
    assert sender.channel_name == "email-team"
    assert hasattr(sender, "send")
    assert inspect.iscoroutinefunction(sender.send)

"""Unit tests for notification models, enums, and config validators (T029, T031).

T029: NotificationHistory query filters, time-based eviction, unique attempt_id.
T031: ChannelConfig and NotificationsConfig validation.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from coordinare.config import ChannelConfig, NotificationsConfig, RoutingEntry
from coordinare.models.notification import (
    EventType,
    NotificationAttempt,
    NotificationHistory,
    NotificationStatus,
)

# ---------------------------------------------------------------------------
# T029: NotificationHistory tests
# ---------------------------------------------------------------------------


def _make_attempt(
    *,
    event_type: EventType = EventType.card_transition,
    channel_name: str = "slack-ops",
    status: NotificationStatus = NotificationStatus.delivered,
    timestamp: datetime | None = None,
) -> NotificationAttempt:
    import uuid

    return NotificationAttempt(
        attempt_id=str(uuid.uuid4()),
        event_type=event_type,
        channel_name=channel_name,
        status=status,
        timestamp=timestamp or datetime.now(UTC),
        elapsed_ms=1.5,
    )


def test_history_query_filters_by_event_type() -> None:
    h = NotificationHistory(max_age_hours=24)
    h.append(_make_attempt(event_type=EventType.card_transition))
    h.append(_make_attempt(event_type=EventType.card_blocked))

    result = h.query(event_type=EventType.card_blocked)
    assert len(result) == 1
    assert result[0].event_type == EventType.card_blocked


def test_history_query_filters_by_channel_name() -> None:
    h = NotificationHistory(max_age_hours=24)
    h.append(_make_attempt(channel_name="slack-ops"))
    h.append(_make_attempt(channel_name="email-team"))

    result = h.query(channel_name="email-team")
    assert len(result) == 1
    assert result[0].channel_name == "email-team"


def test_history_query_filters_by_status() -> None:
    h = NotificationHistory(max_age_hours=24)
    h.append(_make_attempt(status=NotificationStatus.delivered))
    h.append(_make_attempt(status=NotificationStatus.failed))

    result = h.query(status=NotificationStatus.delivered)
    assert len(result) == 1
    assert result[0].status == NotificationStatus.delivered


def test_history_query_filters_by_since() -> None:
    h = NotificationHistory(max_age_hours=24)
    old = datetime.now(UTC) - timedelta(hours=2)
    recent = datetime.now(UTC)
    h.append(_make_attempt(timestamp=old))
    h.append(_make_attempt(timestamp=recent))

    result = h.query(since=datetime.now(UTC) - timedelta(hours=1))
    assert len(result) == 1


def test_history_eviction_removes_old_records() -> None:
    h = NotificationHistory(max_age_hours=1)
    old = datetime.now(UTC) - timedelta(hours=2)
    h.append(_make_attempt(timestamp=old))
    h.append(_make_attempt())

    result = h.query()
    assert len(result) == 1


def test_history_records_after_eviction_retained() -> None:
    h = NotificationHistory(max_age_hours=1)
    old = datetime.now(UTC) - timedelta(hours=2)
    h.append(_make_attempt(timestamp=old))

    # This append triggers eviction of the old record
    new_attempt = _make_attempt()
    h.append(new_attempt)

    result = h.query()
    assert len(result) == 1
    assert result[0].attempt_id == new_attempt.attempt_id


def test_history_attempt_id_is_unique() -> None:
    h = NotificationHistory(max_age_hours=24)
    h.append(_make_attempt())
    h.append(_make_attempt())

    result = h.query()
    ids = [r.attempt_id for r in result]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# T031: ChannelConfig validator tests
# ---------------------------------------------------------------------------


def test_channel_config_rejects_slack_without_webhook_url() -> None:
    with pytest.raises(ValidationError, match="webhook_url required"):
        ChannelConfig(name="bad-slack", type="slack")


def test_channel_config_rejects_email_without_smtp_host() -> None:
    with pytest.raises(ValidationError, match="smtp_host and smtp_recipient required"):
        ChannelConfig(name="bad-email", type="email", smtp_recipient="a@b.com")


def test_channel_config_rejects_email_without_smtp_recipient() -> None:
    with pytest.raises(ValidationError, match="smtp_host and smtp_recipient required"):
        ChannelConfig(name="bad-email", type="email", smtp_host="smtp.example.com")


def test_notifications_config_rejects_duplicate_channel_names() -> None:
    with pytest.raises(ValidationError, match="Channel names must be unique"):
        NotificationsConfig(
            channels=[
                ChannelConfig(name="slack-ops", type="slack", webhook_url="https://hooks.slack.com/a"),
                ChannelConfig(name="slack-ops", type="slack", webhook_url="https://hooks.slack.com/b"),
            ],
        )


def test_channel_config_rejects_malformed_message_template() -> None:
    with pytest.raises(ValidationError, match="Invalid template syntax"):
        ChannelConfig(
            name="slack-ops",
            type="slack",
            webhook_url="https://hooks.slack.com/a",
            message_template="{unclosed",
        )


def test_channel_config_rejects_malformed_subject_template() -> None:
    with pytest.raises(ValidationError, match="Invalid template syntax"):
        ChannelConfig(
            name="slack-ops",
            type="slack",
            webhook_url="https://hooks.slack.com/a",
            subject_template="{also unclosed",
        )


def test_channel_config_accepts_valid_templates() -> None:
    ch = ChannelConfig(
        name="slack-ops",
        type="slack",
        webhook_url="https://hooks.slack.com/a",
        message_template="{event_type}: {summary}",
        subject_template="[Coordinare] {event_type}",
    )
    assert ch.message_template == "{event_type}: {summary}"


def test_notifications_config_rejects_unknown_routing_channel() -> None:
    with pytest.raises(ValidationError, match="unknown channel"):
        NotificationsConfig(
            channels=[
                ChannelConfig(name="slack-ops", type="slack", webhook_url="https://hooks.slack.com/a"),
            ],
            routing=[
                RoutingEntry(event_type=EventType.card_transition, channels=["nonexistent"]),
            ],
        )

from __future__ import annotations

import pytest

from coordinare.graph.nodes.notify import notify
from coordinare.graph.state import initial_state


class _Email:
    def __init__(self):
        self.sent = 0

    async def send_notification(self, recipient, notification):
        _ = (recipient, notification)
        self.sent += 1


class _Slack:
    def __init__(self):
        self.sent = 0

    async def send_notification(self, notification):
        _ = notification
        self.sent += 1


@pytest.mark.asyncio
async def test_notification_pipeline_sends_both_channels() -> None:
    email = _Email()
    slack = _Slack()
    state = initial_state()
    state.update(
        {
            "current_card": {
                "title": "Card",
                "status": "IN_PROGRESS",
                "previous_status": "TODO",
                "description": "Task",
            },
            "email_service": email,
            "slack_service": slack,
            "notification_email": "team@example.com",
        }
    )

    await notify(state)

    assert email.sent == 1
    assert slack.sent == 1


@pytest.mark.asyncio
async def test_notification_latency_target_smoke() -> None:
    email = _Email()
    slack = _Slack()
    state = initial_state()
    state.update(
        {
            "current_card": {
                "title": "Card",
                "status": "IN_PROGRESS",
                "previous_status": "TODO",
                "description": "Task",
            },
            "email_service": email,
            "slack_service": slack,
            "notification_email": "team@example.com",
        }
    )

    await notify(state)

    assert email.sent == 1 and slack.sent == 1

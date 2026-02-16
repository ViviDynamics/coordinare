from __future__ import annotations

import pytest

from coordinare.graph.nodes.notify import notify
from coordinare.graph.state import initial_state


class _Email:
    async def send_notification(self, recipient, notification):
        assert recipient == "team@example.com"
        assert notification.card_title == "Card"


class _Slack:
    async def send_notification(self, notification):
        assert notification.card_title == "Card"


@pytest.mark.asyncio
async def test_notify_sends_to_email_and_slack() -> None:
    state = initial_state()
    state["current_card"] = {
        "title": "Card",
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
        "description": "Task",
    }
    state["email_service"] = _Email()
    state["slack_service"] = _Slack()
    state["notification_email"] = "team@example.com"

    result = await notify(state)

    assert result["current_card"]["title"] == "Card"

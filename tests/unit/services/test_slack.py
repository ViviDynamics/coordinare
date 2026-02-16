from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.models.card import CardStatus
from coordinare.models.notification import Notification
from coordinare.services.slack import SlackService


@pytest.mark.asyncio
async def test_slack_service_posts_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(raise_for_status=lambda: None)

    client = AsyncMock()
    client.post.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    monkeypatch.setattr("coordinare.services.slack.httpx.AsyncClient", lambda timeout=10: client)

    service = SlackService("https://hooks.slack.com/services/x/y/z", "#eng")
    notification = Notification(
        card_title="Card",
        previous_status=CardStatus.TODO,
        card_status=CardStatus.IN_PROGRESS,
        task_description="Task",
    )

    await service.send_notification(notification)

    client.post.assert_awaited_once()

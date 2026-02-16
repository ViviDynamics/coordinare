from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from coordinare.models.notification import Notification


class SlackService:
    def __init__(self, webhook_url: str, channel: str) -> None:
        self._webhook_url = webhook_url
        self._channel = channel

    async def send_notification(self, notification: Notification) -> None:
        payload = {
            "channel": self._channel,
            "text": notification.as_text(),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(self._webhook_url, json=payload)
            response.raise_for_status()

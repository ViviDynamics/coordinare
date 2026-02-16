from __future__ import annotations

from email.message import EmailMessage
from typing import TYPE_CHECKING

import aiosmtplib

if TYPE_CHECKING:
    from coordinare.models.notification import Notification


class EmailService:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str | None = None,
        password: str | None = None,
        sender: str = "coordinare@vividynamics.com",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender

    async def send_notification(self, recipient: str, notification: Notification) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = recipient
        message["Subject"] = f"[Coordinare] {notification.card_title} -> {notification.card_status}"
        message.set_content(notification.as_text())

        await aiosmtplib.send(
            message,
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
        )

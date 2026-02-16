from __future__ import annotations

import pytest

from coordinare.models.card import CardStatus
from coordinare.models.notification import Notification
from coordinare.services.email import EmailService


@pytest.mark.asyncio
async def test_email_service_sends_message(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_send(message, **kwargs):
        captured["subject"] = message["Subject"]
        captured["hostname"] = kwargs["hostname"]

    monkeypatch.setattr("coordinare.services.email.aiosmtplib.send", _fake_send)

    service = EmailService(host="smtp.example.com", port=587)
    notification = Notification(
        card_title="Card",
        previous_status=CardStatus.TODO,
        card_status=CardStatus.IN_PROGRESS,
        task_description="Task",
    )

    await service.send_notification("team@example.com", notification)

    assert "Coordinare" in str(captured["subject"])
    assert captured["hostname"] == "smtp.example.com"

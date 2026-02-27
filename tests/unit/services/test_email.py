from __future__ import annotations

from coordinare.services.email import EmailService


def test_email_service_is_passive_config_holder() -> None:
    """EmailService retains constructor and attributes but no send method."""
    service = EmailService(host="smtp.example.com", port=587)
    assert service._host == "smtp.example.com"
    assert service._port == 587
    assert not hasattr(service, "send_notification")

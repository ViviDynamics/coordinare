from __future__ import annotations

from coordinare.services.slack import SlackService


def test_slack_service_is_passive_config_holder() -> None:
    """SlackService retains constructor and attributes but no send method."""
    service = SlackService("https://hooks.slack.com/services/x/y/z", "#eng")
    assert service._webhook_url == "https://hooks.slack.com/services/x/y/z"
    assert service._channel == "#eng"
    assert not hasattr(service, "send_notification")

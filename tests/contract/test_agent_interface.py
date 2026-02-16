from __future__ import annotations

from coordinare.services.agent_ssh import AgentSSHService


def test_agent_service_shape() -> None:
    service = AgentSSHService(host="host", user="user", command="agent")

    assert hasattr(service, "dispatch_card")
    assert hasattr(service, "check_health")
    assert hasattr(service, "relay_feedback")
    assert hasattr(service, "check_status")

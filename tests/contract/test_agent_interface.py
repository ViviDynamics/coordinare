from __future__ import annotations

from unittest.mock import AsyncMock

from coordinare.services.agent_service import AgentService


def test_agent_service_shape() -> None:
    transport = AsyncMock()
    service = AgentService(transport)

    assert hasattr(service, "dispatch_card")
    assert hasattr(service, "check_health")
    assert hasattr(service, "relay_feedback")
    assert hasattr(service, "check_status")

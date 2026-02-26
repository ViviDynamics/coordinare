from __future__ import annotations

import pytest

from coordinare.graph.nodes.dispatch_card import dispatch_card
from coordinare.graph.state import initial_state


class _GitHub:
    async def move_card(self, item_id: str, status: str) -> None:
        assert item_id == "ITEM_1"
        assert status == "IN_PROGRESS"


class _Agent:
    async def check_health(self):
        return {"status": "accepted"}

    async def dispatch_card(self, card_context):
        _ = card_context
        return {"status": "accepted", "session_id": "s1"}


class _AgentUnhealthy:
    async def check_health(self):
        return {"status": "error", "reason": "Agent is down"}

    async def dispatch_card(self, card_context):
        raise AssertionError("Should not dispatch when unhealthy")


class _AgentUnreachable:
    async def check_health(self):
        raise ConnectionError("Agent unreachable")

    async def dispatch_card(self, card_context):
        raise AssertionError("Should not dispatch when unreachable")


@pytest.mark.asyncio
async def test_dispatch_card_moves_and_dispatches() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _Agent()

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_agent"
    assert result["agent_dispatch"]["status"] == "accepted"
    assert result["agent_health_status"] == "accepted"


@pytest.mark.asyncio
async def test_dispatch_card_blocks_on_unhealthy_agent() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _AgentUnhealthy()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert result["agent_health_status"] == "error"
    assert any("health check failed" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_dispatch_card_blocks_on_unreachable_agent() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _AgentUnreachable()

    result = await dispatch_card(state)

    assert result["phase"] == "blocked"
    assert result["agent_health_status"] == "unreachable"

from __future__ import annotations

import pytest

from coordinare.graph.nodes.dispatch_card import dispatch_card
from coordinare.graph.state import initial_state


class _GitHub:
    async def move_card(self, item_id: str, status: str) -> None:
        assert item_id == "ITEM_1"
        assert status == "IN_PROGRESS"


class _Agent:
    async def dispatch_card(self, card_context):
        _ = card_context
        return {"status": "accepted"}


@pytest.mark.asyncio
async def test_dispatch_card_moves_and_dispatches() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "status": "TODO"}
    state["github_service"] = _GitHub()
    state["agent_service"] = _Agent()

    result = await dispatch_card(state)

    assert result["phase"] == "monitoring_agent"
    assert result["agent_dispatch"]["status"] == "accepted"

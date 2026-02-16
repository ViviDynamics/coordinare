from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_agent import monitor_agent
from coordinare.graph.state import initial_state


class _Agent:
    async def check_status(self, card_id: str):
        _ = card_id
        return {"status": "blocked", "questions": ["Need answer"]}


@pytest.mark.asyncio
async def test_monitor_agent_marks_blocked_with_questions() -> None:
    state = initial_state()
    state["agent_service"] = _Agent()
    state["current_card"] = {"id": "ITEM_1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["Need answer"]

from __future__ import annotations

import pytest

from coordinare.graph.nodes.relay_feedback import relay_feedback
from coordinare.graph.state import initial_state


class _Agent:
    async def relay_feedback(self, review_payload):
        assert "reviews" in review_payload
        return {"status": "acknowledged"}


@pytest.mark.asyncio
async def test_relay_feedback_transitions_to_monitoring_agent() -> None:
    state = initial_state()
    state["agent_service"] = _Agent()
    state["pending_reviews"] = [{"id": "RVW_1"}]

    result = await relay_feedback(state)

    assert result["phase"] == "monitoring_agent"

from __future__ import annotations

import pytest

from coordinare.graph.nodes.relay_feedback import relay_feedback
from coordinare.graph.state import initial_state


class _Agent:
    def __init__(self):
        self.last_payload = None

    async def relay_feedback(self, review_payload):
        self.last_payload = review_payload
        return {"status": "acknowledged"}


@pytest.mark.asyncio
async def test_relay_feedback_transitions_to_monitoring_agent() -> None:
    agent = _Agent()
    state = initial_state()
    state["agent_service"] = agent
    state["pending_reviews"] = [{"id": "RVW_1"}]
    state["agent_dispatch"] = {"session_id": "s1"}
    state["current_card"] = {"pr_url": "https://github.com/org/repo/pull/1"}

    result = await relay_feedback(state)

    assert result["phase"] == "monitoring_agent"
    assert agent.last_payload["session_id"] == "s1"
    assert agent.last_payload["pr_url"] == "https://github.com/org/repo/pull/1"
    assert agent.last_payload["reviews"] == [{"id": "RVW_1"}]


@pytest.mark.asyncio
async def test_relay_feedback_returns_monitoring_pr_when_no_agent() -> None:
    state = initial_state()
    state["pending_reviews"] = [{"id": "RVW_1"}]

    result = await relay_feedback(state)

    assert result["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_relay_feedback_skips_relay_when_reviews_empty() -> None:
    """Guard: agent.relay_feedback must not be called with an empty reviews list."""
    called = False

    class _AgentShouldNotBeCalled:
        async def relay_feedback(self, review_payload):
            nonlocal called
            called = True
            return {"status": "acknowledged"}

    state = initial_state()
    state["agent_service"] = _AgentShouldNotBeCalled()
    state["pending_reviews"] = []

    result = await relay_feedback(state)

    assert not called, "relay_feedback should not be called when pending_reviews is empty"
    assert result["phase"] == "monitoring_agent"

from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_agent import monitor_agent
from coordinare.graph.state import initial_state


class _Agent:
    async def check_status(self, session_id: str):
        _ = session_id
        return {"status": "blocked", "questions": ["Need answer"]}


@pytest.mark.asyncio
async def test_monitor_agent_marks_blocked_with_questions() -> None:
    state = initial_state()
    state["agent_service"] = _Agent()
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["Need answer"]


@pytest.mark.asyncio
async def test_monitor_agent_idle_when_no_agent() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1"}

    result = await monitor_agent(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_monitor_agent_idle_when_no_card() -> None:
    state = initial_state()
    state["agent_service"] = _Agent()

    result = await monitor_agent(state)

    assert result["phase"] == "idle"


class _AgentPrOpened:
    async def check_status(self, session_id: str):
        return {
            "status": "pr_opened",
            "pr_url": "https://github.com/org/repo/pull/1",
            "pr_node_id": "PR_NODE_1",
        }


@pytest.mark.asyncio
async def test_monitor_agent_transitions_to_monitoring_pr_on_pr_opened() -> None:
    state = initial_state()
    state["agent_service"] = _AgentPrOpened()
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_pr"
    assert result["current_card"]["pr_url"] == "https://github.com/org/repo/pull/1"
    assert result["current_card"]["pr_node_id"] == "PR_NODE_1"
    assert result["current_card"]["status"] == "IN_REVIEW"
    assert result["current_card"]["previous_status"] == "IN_PROGRESS"


class _AgentWorking:
    async def check_status(self, session_id: str):
        return {"status": "working"}


@pytest.mark.asyncio
async def test_monitor_agent_stays_monitoring_when_working() -> None:
    state = initial_state()
    state["agent_service"] = _AgentWorking()
    state["current_card"] = {"id": "ITEM_1"}
    state["agent_dispatch"] = {"session_id": "s1"}

    result = await monitor_agent(state)

    assert result["phase"] == "monitoring_agent"

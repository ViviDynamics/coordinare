from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


async def monitor_agent(state: CoordinareState) -> CoordinareState:
    agent = state.get("agent_service")
    card = state.get("current_card")
    if agent is None or not isinstance(card, dict):
        state["phase"] = "idle"
        return state

    status = await agent.check_status(str(card.get("id", "")))
    marker = status.get("status", "working")
    if marker == "pr_opened":
        card["pr_url"] = status.get("pr_url")
        card["pr_node_id"] = status.get("pr_node_id")
        card["previous_status"] = card.get("status", "IN_PROGRESS")
        card["status"] = "IN_REVIEW"
        state["current_card"] = card
        state["phase"] = "monitoring_pr"
    elif marker in {"blocked", "error"}:
        state["phase"] = "blocked"
        questions = status.get("questions")
        state["open_questions"] = [str(item) for item in questions] if isinstance(questions, list) else []
    else:
        state["phase"] = "monitoring_agent"
    return state

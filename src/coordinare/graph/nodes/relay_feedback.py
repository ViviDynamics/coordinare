from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


async def relay_feedback(state: CoordinareState) -> CoordinareState:
    agent = state.get("agent_service")
    reviews = state.get("pending_reviews", [])
    if agent is None:
        state["phase"] = "monitoring_pr"
        return state
    if not reviews:
        state["phase"] = "monitoring_agent"
        return state
    card = state.get("current_card") or {}
    session_id = state.get("agent_dispatch", {}).get("session_id", "")
    pr_url = card.get("pr_url", "")
    await agent.relay_feedback({
        "session_id": session_id,
        "pr_url": pr_url,
        "reviews": reviews,
    })
    state["phase"] = "monitoring_agent"
    return state

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


async def dispatch_card(state: CoordinareState) -> CoordinareState:
    card = state.get("current_card")
    github = state.get("github_service")
    agent = state.get("agent_service")
    if not isinstance(card, dict) or github is None or agent is None:
        state["phase"] = "idle"
        return state

    card_id = str(card.get("id", ""))
    await github.move_card(card_id, "IN_PROGRESS")
    result = await agent.dispatch_card(card)
    state["agent_dispatch"] = result
    card["previous_status"] = card.get("status", "TODO")
    card["status"] = "IN_PROGRESS"
    state["current_card"] = card
    state["phase"] = "monitoring_agent"
    return state

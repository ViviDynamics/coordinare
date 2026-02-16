from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


async def merge_pr(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    card = state.get("current_card")
    if github is None or not isinstance(card, dict):
        state["phase"] = "idle"
        return state

    pr_node_id = str(card.get("pr_node_id", ""))
    mergeability = await github.check_mergeability(pr_node_id)
    if not mergeability.get("mergeable", False):
        state["phase"] = "blocked"
        state["open_questions"] = ["PR has merge conflicts requiring human intervention."]
        return state

    await github.squash_merge(pr_node_id)
    await github.move_card(str(card.get("id", "")), "DONE")
    card["previous_status"] = card.get("status", "IN_REVIEW")
    card["status"] = "DONE"
    state["current_card"] = card
    state["phase"] = "idle"
    return state

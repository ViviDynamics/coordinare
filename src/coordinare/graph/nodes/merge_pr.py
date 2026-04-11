from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def merge_pr(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    card = state.get("current_card")
    if github is None or not isinstance(card, dict):
        state["phase"] = "idle"
        return state

    pr_node_id = str(card.get("pr_node_id", ""))
    try:
        mergeability = await github.check_mergeability(pr_node_id)
    except Exception as exc:
        logger.warning("merge_pr.check_mergeability_failed", error=str(exc))
        state["phase"] = "merging"  # retry next cycle
        return state
    if not mergeability.get("mergeable", False):
        state["phase"] = "blocked"
        state["open_questions"] = ["PR has merge conflicts requiring human intervention."]
        return state

    try:
        merge_result = await github.squash_merge(pr_node_id)
    except Exception as exc:
        logger.warning("merge_pr.squash_merge_failed", error=str(exc))
        state["phase"] = "merging"  # retry next cycle
        return state
    merge_commit = merge_result.get("merge_commit")
    if isinstance(merge_commit, dict):
        oid = str(merge_commit.get("oid", ""))
        headline = str(merge_commit.get("messageHeadline", ""))
        state["commit_summary"] = f"{oid[:7]} {headline}".strip() if oid else (headline or None)
    else:
        state["commit_summary"] = None

    try:
        await github.move_card(str(card.get("id", "")), "DONE")
    except Exception as exc:
        logger.warning("merge_pr.move_card_failed", error=str(exc))
    card["previous_status"] = card.get("status", "IN_REVIEW")
    card["status"] = "DONE"
    state["current_card"] = card
    state["phase"] = "idle"
    return state

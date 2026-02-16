from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
from coordinare.models.review import ReviewerType, classify_reviewer


async def monitor_pr(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    card = state.get("current_card")
    if github is None or not isinstance(card, dict):
        state["phase"] = "idle"
        return state

    reviews = await github.get_pr_reviews(str(card.get("pr_node_id", "")))
    human_reviewers = state.get("human_reviewers", [])
    actionable: list[dict[str, object]] = []
    approved = False
    for review in reviews:
        login = str(review.get("author_login", ""))
        reviewer_type = classify_reviewer(login, human_reviewers if isinstance(human_reviewers, list) else [])
        review["author_type"] = ReviewerType(reviewer_type)
        if reviewer_type == ReviewerType.HUMAN and review.get("state") == "APPROVED":
            approved = True
        if reviewer_type == ReviewerType.HUMAN and review.get("state") in {"COMMENTED", "CHANGES_REQUESTED"}:
            actionable.append(review)

    state["pending_reviews"] = actionable
    if approved:
        state["phase"] = "merging"
    elif actionable:
        state["phase"] = "relay_feedback"
    else:
        state["phase"] = "monitoring_pr"
    return state

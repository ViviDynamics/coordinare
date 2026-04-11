from __future__ import annotations

import contextlib
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
from coordinare.models.review import ReviewerType, classify_reviewer

logger = structlog.get_logger(__name__)


async def monitor_pr(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    card = state.get("current_card")
    if github is None or not isinstance(card, dict):
        state["phase"] = "idle"
        return state

    pr_node_id = str(card.get("pr_node_id") or "")
    if not pr_node_id:
        state["phase"] = "idle"
        return state

    try:
        reviews = await github.get_pr_reviews(pr_node_id)
    except Exception as exc:
        logger.warning("monitor_pr.get_reviews_failed", error=str(exc))
        state["phase"] = "monitoring_pr"  # stay in current phase, retry next cycle
        return state
    human_reviewers = state.get("human_reviewers", [])

    # Only consider reviews submitted after the lifecycle completed.
    # This prevents old reviews (already addressed by the performer lifecycle)
    # from re-triggering classify_human_feedback in a loop.
    lifecycle_completed_at = state.get("lifecycle_completed_at")
    cutoff: datetime | None = None
    if isinstance(lifecycle_completed_at, datetime):
        cutoff = lifecycle_completed_at
    elif isinstance(lifecycle_completed_at, str) and lifecycle_completed_at:
        with contextlib.suppress(ValueError, TypeError):
            cutoff = datetime.fromisoformat(lifecycle_completed_at.replace("Z", "+00:00"))

    actionable: list[dict[str, object]] = []
    approved = False
    for review in reviews:
        login = str(review.get("author_login", ""))
        reviewer_type = classify_reviewer(login, human_reviewers if isinstance(human_reviewers, list) else [])
        review["author_type"] = ReviewerType(reviewer_type)

        # Filter out reviews submitted before the lifecycle completed.
        if cutoff is not None:
            submitted_raw = review.get("submitted_at", "")
            if isinstance(submitted_raw, str) and submitted_raw:
                try:
                    submitted_at = datetime.fromisoformat(submitted_raw.replace("Z", "+00:00"))
                    if submitted_at <= cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

        if reviewer_type == ReviewerType.HUMAN and review.get("state") == "APPROVED":
            approved = True
        if reviewer_type == ReviewerType.HUMAN and review.get("state") in {"COMMENTED", "CHANGES_REQUESTED"}:
            actionable.append(review)

    state["pending_reviews"] = actionable
    if approved:
        state["phase"] = "merging"
    elif actionable:
        logger.info(
            "monitor_pr.actionable_reviews",
            count=len(actionable),
            cutoff=cutoff.isoformat() if cutoff else None,
        )
        state["phase"] = "relay_feedback"
    else:
        state["phase"] = "monitoring_pr"
    return state

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

    # ``"None"`` (the literal string) can land here from older snapshots
    # written before the daemon snapshot serialisation bug was fixed —
    # treat it the same as missing so we attempt PR recovery rather than
    # querying GitHub with a string that resolves to no node.
    raw_pr_node_id = card.get("pr_node_id")
    pr_node_id = str(raw_pr_node_id or "")
    if pr_node_id in ("", "None", "null"):
        pr_node_id = ""

    if not pr_node_id:
        # Recovery path: state lacks a usable pr_node_id but the card sits in
        # IN_REVIEW, meaning a PR exists somewhere — find it via the issue's
        # "Closes #N" linkage and rehydrate the card so monitoring continues.
        issue_node_id = str(card.get("issue_id") or "")
        recovered = None
        if issue_node_id:
            try:
                recovered = await github.find_pr_for_issue(issue_node_id)
            except Exception as exc:
                logger.warning(
                    "monitor_pr.pr_recovery_failed",
                    issue_node_id=issue_node_id,
                    error=str(exc),
                )
        if recovered:
            card["pr_node_id"] = recovered["pr_node_id"]
            card["pr_url"] = recovered["pr_url"]
            state["current_card"] = card
            pr_node_id = recovered["pr_node_id"]
            logger.info(
                "monitor_pr.pr_recovered",
                pr_node_id=pr_node_id,
                pr_url=recovered["pr_url"],
                issue_node_id=issue_node_id,
            )
        else:
            logger.warning(
                "monitor_pr.no_pr_node_id_and_no_recovery",
                issue_node_id=issue_node_id,
                card_id=str(card.get("id", "")),
            )
            state["phase"] = "idle"
            return state

    try:
        reviews = await github.get_pr_reviews(pr_node_id)
    except Exception as exc:
        logger.warning("monitor_pr.get_reviews_failed", error=str(exc))
        state["phase"] = "monitoring_pr"  # stay in current phase, retry next cycle
        return state
    human_reviewers = state.get("human_reviewers", [])
    trusted_bot_reviewers = state.get("trusted_bot_reviewers", [])

    # Filter reviews using two mechanisms:
    # 1. lifecycle_completed_at timestamp — reviews before this are from a prior cycle
    # 2. processed_review_ids — reviews already dispatched (survives cutoff clears)
    lifecycle_completed_at = state.get("lifecycle_completed_at")
    cutoff: datetime | None = None
    if isinstance(lifecycle_completed_at, datetime):
        cutoff = lifecycle_completed_at
    elif isinstance(lifecycle_completed_at, str) and lifecycle_completed_at:
        with contextlib.suppress(ValueError, TypeError):
            cutoff = datetime.fromisoformat(lifecycle_completed_at.replace("Z", "+00:00"))

    processed_ids: set[str] = state.get("processed_review_ids") or set()

    actionable: list[dict[str, object]] = []
    approved = False
    for review in reviews:
        review_id = str(review.get("id", ""))
        login = str(review.get("author_login", ""))
        reviewer_type = classify_reviewer(
            login,
            human_reviewers if isinstance(human_reviewers, list) else [],
            trusted_bot_reviewers if isinstance(trusted_bot_reviewers, list) else [],
        )
        # Store the enum's value (a plain string like "HUMAN") rather than
        # the enum instance — relay_feedback dicts are JSON-serialized via
        # json.dumps(..., default=str), and enum __str__ gives
        # "ReviewerType.HUMAN" instead of the intended stable "HUMAN".
        review["author_type"] = ReviewerType(reviewer_type).value
        is_actionable_type = reviewer_type in (ReviewerType.HUMAN, ReviewerType.TRUSTED_BOT)

        # Skip reviews already processed in a previous dispatch cycle
        if review_id and review_id in processed_ids:
            continue

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

        # Only HUMAN approval triggers merge — trusted bots can provide
        # feedback but cannot approve on behalf of a human.
        if reviewer_type == ReviewerType.HUMAN and review.get("state") == "APPROVED":
            approved = True
        if is_actionable_type and review.get("state") in {"COMMENTED", "CHANGES_REQUESTED"}:
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

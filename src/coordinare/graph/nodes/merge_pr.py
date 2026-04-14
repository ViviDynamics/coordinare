from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from coordinare.services.github import PermanentGitHubError

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
    except PermanentGitHubError as exc:
        # 042: Permanent error means GitHub will reject this call identically
        # on every retry (e.g., bad node id, missing permission).  Stop the
        # retry loop and surface to the operator via blocked phase.
        logger.warning("merge_pr.check_mergeability_permanent_error", error=str(exc))
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Cannot check PR mergeability — GitHub rejected the request: {exc}",
        ]
        return state
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
    except PermanentGitHubError as exc:
        # 042: Most common case is the GitHub App lacking ruleset bypass
        # permission — "You're not authorized to push to this branch".
        # Looping forever spams the merge endpoint and produces no merge.
        # Mark blocked with the actual GitHub error so the operator can
        # add the App to the ruleset bypass list.
        logger.warning("merge_pr.squash_merge_permanent_error", error=str(exc))
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Cannot merge PR — GitHub rejected the merge request: {exc}",
        ]
        return state
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

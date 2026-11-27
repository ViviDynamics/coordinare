"""Find a work item's open pull request again after state lost the link (#397).

A GitHub issue's pull request is found through its closing references: the
``Closes #N`` its body carries. A work item from another tracker has no issue
and no closing reference, but its branch is ``coordinare/{card id}/{slug}``
(``dispatch_guard.canonical_branch_name``), so its open pull request is the one
whose head starts with that prefix.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def branch_prefix(card_id: str) -> str:
    """The head ref prefix every pull request for *card_id* is opened from."""
    return f"coordinare/{card_id}/"


async def find_pr_for_card(github: Any, card: dict[str, Any], issue_node_id: str) -> dict[str, str] | None:
    """The card's open pull request as ``{"pr_node_id", "pr_url"}``, or None.

    A card that carries a work item reference (397: an item with no GitHub
    issue) is matched by branch prefix. Any other card is looked up by its issue's
    closing references, exactly as before.
    """
    if not card.get("work_item_ref"):
        found: dict[str, str] | None = await github.find_pr_for_issue(issue_node_id)
        return found
    card_id = str(card.get("id") or "")
    owner = str(getattr(github, "org", "") or "")
    repo = str(getattr(github, "project_name", "") or "")
    if not (card_id and owner and repo):
        return None
    prefix = branch_prefix(card_id)
    for pr in await github.list_open_pull_requests(owner, repo):
        head = str(pr.get("head_ref") or "")
        if head.startswith(prefix) and pr.get("id") and pr.get("url"):
            logger.info("pr_recovery.found_by_branch", card_id=card_id, pr_url=pr.get("url"))
            return {"pr_node_id": str(pr["id"]), "pr_url": str(pr["url"])}
    return None

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


def _named_by_branch(card: dict[str, Any]) -> bool:
    """Whether *card* is a work item with no GitHub issue (397).

    It carries a reference, or (a card restored before it regained one) it is
    addressed by its own id rather than by an issue node id. A GitHub card's
    issue id is a node id, never its project item id.
    """
    if card.get("work_item_ref"):
        return True
    card_id = str(card.get("id") or "")
    return bool(card_id) and not card.get("issue_number") and str(card.get("issue_id") or "") == card_id


async def find_pr_for_card(github: Any, card: dict[str, Any], issue_node_id: str) -> dict[str, str] | None:
    """The card's open pull request as ``{"pr_node_id", "pr_url"}``, or None.

    A work item with no GitHub issue is matched by branch prefix, and only on a
    pull request opened from the repository itself: a fork can name a branch
    anything. More than one match is divergence, which dispatch reports, so
    nothing is adopted. Any other card is looked up by its issue's closing
    references, exactly as before.
    """
    if not _named_by_branch(card):
        found: dict[str, str] | None = await github.find_pr_for_issue(issue_node_id)
        return found
    card_id = str(card.get("id") or "")
    owner = str(getattr(github, "org", "") or "")
    repo = str(getattr(github, "project_name", "") or "")
    if not (card_id and owner and repo):
        return None
    prefix = branch_prefix(card_id)
    own_repo = f"{owner}/{repo}".lower()
    matches = [
        pr for pr in await github.list_open_pull_requests(owner, repo)
        if str(pr.get("head_ref") or "").startswith(prefix)
        and str(pr.get("head_repo") or "").lower() == own_repo
        and pr.get("id") and pr.get("url")
    ]
    if len(matches) != 1:
        if matches:
            logger.warning("pr_recovery.ambiguous_branch", card_id=card_id, count=len(matches))
        return None
    (pr,) = matches
    logger.info("pr_recovery.found_by_branch", card_id=card_id, pr_url=pr.get("url"))
    return {"pr_node_id": str(pr["id"]), "pr_url": str(pr["url"])}

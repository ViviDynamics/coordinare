"""Fresh fail-safe lifecycle reads before retained PR feedback is retried."""
from __future__ import annotations

from typing import Any


async def retained_pr_is_open(github: Any, card: dict[str, Any]) -> bool:
    """Unknown or unreadable PR state cannot authorize a replacement worker."""
    pr_node_id = card.get("pr_node_id")
    if github is None or not pr_node_id:
        return False
    try:
        context = await github.get_pr_review_context(str(pr_node_id))
    except Exception:
        return False
    return isinstance(context, dict) and context.get("state") == "OPEN"

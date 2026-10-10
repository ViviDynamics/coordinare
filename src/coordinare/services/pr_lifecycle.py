"""Fresh fail-safe lifecycle reads before retained PR feedback is retried."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coordinare.graph.nodes.github_retry import (
    clear_deferred_github_operation,
    defer_github_operation,
    github_operation_ready,
    is_transient_github_outage_error,
)

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


async def retained_pr_is_open(github: Any, card: dict[str, Any], *, state: CoordinareState) -> bool:
    """Share the existing PR-monitor backoff; unreadable state cannot authorize retry."""
    pr_node_id = card.get("pr_node_id")
    if github is None or not pr_node_id:
        return False
    ready, _retry_in = github_operation_ready(state, "monitor_pr")
    if not ready:
        return False
    try:
        context = await github.get_pr_review_context(str(pr_node_id))
    except Exception as exc:
        if is_transient_github_outage_error(exc):
            defer_github_operation(state, operation="monitor_pr", error=exc)
        return False
    clear_deferred_github_operation(state, "monitor_pr")
    return isinstance(context, dict) and context.get("state") == "OPEN"

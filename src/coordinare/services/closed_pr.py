"""Identify the durable operator block for an externally closed unmerged PR."""
from __future__ import annotations

from typing import Any

CLOSED_PR_REASON_PREFIX = "PR closed without merging:"


def is_closed_pr_block(reason: object) -> bool:
    return isinstance(reason, str) and reason.startswith(CLOSED_PR_REASON_PREFIX)


def matches_closed_pr_identity(reason: str, recovered: dict[str, Any]) -> bool:
    """Match the exact URL or node ID recorded when the unmerged PR closed."""
    return any(
        identity and reason.startswith(f"{CLOSED_PR_REASON_PREFIX} {identity}. ")
        for identity in (recovered.get("pr_url"), recovered.get("pr_node_id"))
    )

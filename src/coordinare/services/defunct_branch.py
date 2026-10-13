"""89 — defunct branch cleanup for conductor-closed PRs.

When the conductor closes a pull request — by merging it, or by the 553
orphan reconciliation closing a superseded PR — the head branch is defunct.
Nothing cleaned it up at close time: the 052 stale-branch cleanup only runs
lazily at the card's next dispatch, so unre-dispatched cards accumulated
dangling ``coordinare/<card_id>/...`` branches forever.

Opt-in via ``ProjectConfiguration.delete_defunct_branches`` (default off,
SC-003: byte-identical to a pre-89 build when disabled). Deletion reuses the
contractually non-raising ``GitHubService.delete_branch``; this module only
resolves the branch name and logs one decision line per attempt.
"""
from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def resolve_branch(*candidates: Any) -> str:
    """First non-empty string among *candidates*; ``""`` when none qualify."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


async def delete_defunct_branch(
    github: Any,
    branch: str,
    *,
    card_id: str,
    source: str,
    enabled: bool,
) -> bool:
    """Delete one defunct head branch; never raises.

    ``source`` names the close path for the decision log ("merge", "orphan").
    Returns True when the deletion succeeded (including "already gone"),
    False when skipped or refused. Doubles/protocols without
    ``delete_branch`` are skipped silently — the capability is optional by
    contract, mirroring the ``inspect.signature()`` convention for
    cross-protocol calls.
    """
    if not enabled or not branch:
        return False
    delete_branch = getattr(github, "delete_branch", None)
    if not callable(delete_branch):
        return False
    try:
        await delete_branch(branch)
    except Exception as exc:
        logger.warning(
            "defunct_branch.delete_failed",
            card_id=card_id,
            source=source,
            branch=branch,
            error=str(exc),
        )
        return False
    logger.info(
        "defunct_branch.deleted",
        card_id=card_id,
        source=source,
        branch=branch,
    )
    return True

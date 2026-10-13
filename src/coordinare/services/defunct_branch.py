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

Guards (89 review round): the branch is only deleted when its head
repository matches the configured one — fork PRs are never touched — and
when no other live PR still uses it (``branch_has_open_pr`` True or None
preserves the branch).
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


def configured_repo_of(github: Any) -> str:
    """``org/repo`` of the configured repository, ``""`` when unknown.

    Mirrors the attribute chain ``reconcile_completed_card`` uses for
    cross-protocol doubles (private names preferred, public fallbacks).
    """
    org = getattr(github, "_org", None) or getattr(github, "org", None)
    project = getattr(github, "_project_name", None) or getattr(github, "project_name", None)
    return f"{org}/{project}" if org and project else ""


async def delete_defunct_branch(
    github: Any,
    branch: str,
    *,
    card_id: str,
    source: str,
    enabled: bool,
    head_repo: str = "",
    expected_repo: str = "",
) -> bool:
    """Delete one defunct head branch; never raises.

    ``source`` names the close path for the decision log ("merge", "orphan").
    Returns True when the deletion succeeded (including "already gone"),
    False when skipped or refused. Doubles/protocols without
    ``delete_branch`` are skipped silently — the capability is optional by
    contract, mirroring the ``inspect.signature()`` convention for
    cross-protocol calls.

    The deletion is refused (and logged) when the PR's head repository does
    not match the configured one, or when ``branch_has_open_pr`` reports
    another live PR on the branch (True) or is indeterminate (None) — a
    branch can back more than one PR.
    """
    if not enabled or not branch:
        return False
    if not expected_repo or head_repo != expected_repo:
        logger.info(
            "defunct_branch.delete_skipped",
            card_id=card_id,
            source=source,
            branch=branch,
            reason="head_repo_mismatch",
            head_repo=head_repo,
            expected_repo=expected_repo,
        )
        return False
    branch_has_open_pr = getattr(github, "branch_has_open_pr", None)
    if callable(branch_has_open_pr):
        open_pr_state = await branch_has_open_pr(branch)
        if open_pr_state or open_pr_state is None:
            logger.info(
                "defunct_branch.delete_skipped",
                card_id=card_id,
                source=source,
                branch=branch,
                reason="open_pr" if open_pr_state else "open_pr_unknown",
            )
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

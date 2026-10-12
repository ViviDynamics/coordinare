"""553 — Orphaned PR reconciliation for completed cards (issue #78).

A scenario re-run or dispatch retry can leave open PRs whose board card has
already completed via a different (winning) PR. Every session-scoped safety
net (rebase round, multi-PR divergence, bounce routing) iterates
``state["active_sessions"]`` only, so once the card leaves the active set its
losing PRs dangle open forever — mergeStateStatus drifts to DIRTY/UNKNOWN and
the repo accumulates PR clutter that human reviewers cannot tell apart from
live work.

This module is the opt-in janitor. Attribution resolves an open PR to its
board card by (a) session-branch name — canonical ``coordinare/<card_id>/``
or legacy ``conductor/<card_id>/`` — or (b) the PR body's issue reference.
Default-off everywhere: when the config switches are closed, no code path
here runs and behavior is byte-identical to a pre-553 build (SC-003).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)

# Session branches embed the project-item node id directly. The legacy
# ``conductor/`` prefix predates the 076 canonicalization and still identifies
# a card branch (observed in live deployments: conductor/PVTI_.../<slug>).
_SESSION_BRANCH_RE = re.compile(r"^(?:coordinare|conductor)/(?P<node_id>PVTI_[A-Za-z0-9_-]+?)/")
# Issue references in a PR body: "#19" or "org/repo#19".
_BODY_ISSUE_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(\d+)")


def is_terminal_card_status(status: str | None) -> bool:
    """True when the board card can no longer accept work (issue #78)."""
    if not isinstance(status, str):
        return False
    return status.strip().upper() in {"DONE", "CLOSED"}


@dataclass(frozen=True)
class OrphanAttribution:
    """How an open PR maps to a board card (or why it does not)."""

    pr: dict[str, Any]
    card_item_id: str | None
    method: Literal["session_branch", "body_reference", "unresolved"]


def build_close_comment(
    card: dict[str, Any],
    basis: Literal["session_branch", "body_reference", "unresolved"],
) -> str:
    """Assemble the explanatory comment posted before a superseded PR closes.

    FR-004: the comment must name the card, its terminal state, the
    superseding (winning) PR when one exists, and carry the
    ``coordinare-attribution`` marker so operators can trace the closure.
    """
    issue_number = card.get("issue_number")
    title = card.get("title") or card.get("id") or "the card"
    status = str(card.get("status", "DONE")).strip().upper()
    issue_ref = f"#{issue_number}" if isinstance(issue_number, int) else str(card.get("id", ""))
    pr_url = card.get("pr_url")
    lines: list[str] = []
    if isinstance(pr_url, str) and pr_url:
        lines.append(
            f"Card {issue_ref} ({title}) completed via {pr_url}, which was merged. "
            f"This pull request was left open when the card finished; the card is "
            f"{status} and no further work will land here."
        )
    else:
        lines.append(
            f"Card {issue_ref} ({title}) is {status} on the board without a merged "
            f"pull request, so no further work will land here."
        )
    lines.append(
        f"Closing as superseded. (coordinare-attribution: 553 orphan "
        f"reconciliation — card {card.get('id', 'unknown')}, basis {basis})"
    )
    return "\n\n".join(lines)


def resolve_pr_to_card(
    pr: dict[str, Any],
    board_cards: dict[str, dict[str, Any]],
) -> OrphanAttribution:
    """Attribute one open PR to a board card (data-model §2).

    ``board_cards`` maps project-item id → card dict with at least ``id``,
    ``status``, ``issue_number``, ``content_node_id``. Resolution chain:
    session-branch node id first (strongest signal), then the PR body's issue
    reference. Unresolved PRs are never touched (FR-006).
    """
    head_ref = pr.get("head_ref")
    if isinstance(head_ref, str):
        match = _SESSION_BRANCH_RE.match(head_ref)
        if match:
            node_id = match.group("node_id")
            for item_id, card in board_cards.items():
                if item_id == node_id or card.get("content_node_id") == node_id:
                    return OrphanAttribution(pr=pr, card_item_id=item_id, method="session_branch")

    body = pr.get("body")
    if isinstance(body, str):
        for match in _BODY_ISSUE_RE.finditer(body):
            issue_number = int(match.group(1))
            for item_id, card in board_cards.items():
                if card.get("issue_number") == issue_number:
                    return OrphanAttribution(pr=pr, card_item_id=item_id, method="body_reference")

    return OrphanAttribution(pr=pr, card_item_id=None, method="unresolved")

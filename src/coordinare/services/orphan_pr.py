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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from coordinare.config import OrphanPrConfig
    from coordinare.graph.state import GitHubServiceProtocol

logger = structlog.get_logger(__name__)

# Session branches embed the project-item node id directly. The legacy
# ``conductor/`` prefix predates the 076 canonicalization and still identifies
# a card branch (observed in live deployments: conductor/PVTI_.../<slug>).
_SESSION_BRANCH_RE = re.compile(r"^(?:coordinare|conductor)/(?P<node_id>PVTI_[A-Za-z0-9_-]+?)/")
# Issue references in a PR body: "#19" or "org/repo#19" (qualifier captured so
# cross-repo references cannot attribute to this repo's cards).
_BODY_ISSUE_RE = re.compile(r"(?:(?P<qualifier>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<number>\d+)")


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
            f"{status} and no further work will land here.",
        )
    else:
        lines.append(
            f"Card {issue_ref} ({title}) is {status} on the board without a merged "
            f"pull request, so no further work will land here.",
        )
    lines.append(
        f"Closing as superseded. (coordinare-attribution: 553 orphan "
        f"reconciliation — card {card.get('id', 'unknown')}, basis {basis})",
    )
    return "\n\n".join(lines)


def resolve_pr_to_card(
    pr: dict[str, Any],
    board_cards: dict[str, dict[str, Any]],
    expected_qualifier: str | None = None,
) -> OrphanAttribution:
    """Attribute one open PR to a board card (data-model §2).

    ``board_cards`` maps project-item id → card dict with at least ``id``,
    ``status``, ``issue_number``, ``content_node_id``. Resolution chain:
    session-branch node id first (strongest signal), then the PR body's issue
    reference.     Repository-qualified references (``org/repo#19``) only count
    when the qualifier matches ``expected_qualifier``; unqualified references
    are accepted only when the card's board issue URL also lives in the
    expected repository (an org Project can span repositories, and issue
    numbers are per-repo). Unresolved PRs are never touched (FR-006).
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
            qualifier = match.group("qualifier")
            if qualifier and expected_qualifier and qualifier != expected_qualifier:
                continue
            issue_number = int(match.group("number"))
            for item_id, card in board_cards.items():
                if card.get("issue_number") != issue_number:
                    continue
                if expected_qualifier and not qualifier:
                    # Unqualified "#N" is ambiguous across an org Project that
                    # spans repositories — issue numbers are per-repo. Only a
                    # card whose board issue URL lives in the expected
                    # repository can absorb the reference.
                    issue_url = str(card.get("issue_url") or "")
                    if not issue_url.startswith(f"https://github.com/{expected_qualifier}/issues/"):
                        continue
                return OrphanAttribution(pr=pr, card_item_id=item_id, method="body_reference")

    return OrphanAttribution(pr=pr, card_item_id=None, method="unresolved")


def _cards_from_board(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten ``poll_board``'s parallel maps into item id → card dicts.

    The card shape here is the sweep's own minimal projection (id, status,
    title, issue_number, issue_url, content_node_id, pr_url) — deliberately not
    ``check_board._build_card_dict`` so the janitor carries no node imports.
    """
    cards: dict[str, dict[str, Any]] = {}
    snapshot = board.get("snapshot") or {}
    titles = board.get("titles") or {}
    issue_numbers = board.get("issue_numbers") or {}
    issue_urls = board.get("issue_urls") or {}
    content_node_ids = board.get("content_node_ids") or {}
    pr_urls = board.get("pr_urls") or {}
    for status, members in snapshot.items():
        if not isinstance(members, list):
            continue
        for item_id in members:
            if not isinstance(item_id, str) or not item_id:
                continue
            cards[item_id] = {
                "id": item_id,
                "status": str(status),
                "title": titles.get(item_id),
                "issue_number": issue_numbers.get(item_id),
                "issue_url": issue_urls.get(item_id, ""),
                "content_node_id": content_node_ids.get(item_id),
                "pr_url": pr_urls.get(item_id),
            }
    return cards


async def run_orphan_pr_sweep(
    github: GitHubServiceProtocol,
    board: dict[str, Any],
    orphan_cfg: OrphanPrConfig | None,
    active_card_ids: set[str] | None = None,
) -> list[OrphanPrDecision]:
    """Per-cycle janitor (US-2): close open PRs of terminal-card work.

    Disabled → returns [] silently without touching GitHub at all
    (byte-identical to a pre-553 build, SC-003). Enabled → one GitHub read
    (open-PR inventory), then attribution against the board snapshot supplied
    by the caller; every evaluated PR yields one ``orphan_pr.decision`` log
    line (FR-005). Cards with a live session (FR-006) are never touched even
    when the board snapshot shows a terminal column.
    """
    if orphan_cfg is None or not (orphan_cfg.enabled and orphan_cfg.sweep):
        return []
    owner = getattr(github, "_org", None) or getattr(github, "org", None)
    repo = getattr(github, "_project_name", None) or getattr(github, "project_name", None)
    if not owner or not repo:
        logger.warning("orphan_pr.sweep_no_repo_context")
        return []
    open_prs = await github.list_open_pull_requests(owner, repo)
    cards = _cards_from_board(board)
    qualifier = f"{owner}/{repo}"
    live = active_card_ids or set()
    decisions: list[OrphanPrDecision] = []
    for pr in open_prs:
        attribution = resolve_pr_to_card(pr, cards, expected_qualifier=qualifier)
        if attribution.card_item_id is None:
            decisions.append(_log_decision(_decision(pr, None, attribution.method, "skipped-unresolved")))
            continue
        card = cards[attribution.card_item_id]
        if not is_terminal_card_status(card.get("status")) or attribution.card_item_id in live:
            decisions.append(
                _log_decision(_decision(pr, attribution.card_item_id, attribution.method, "skipped-active")),
            )
            continue
        decisions.extend(await close_superseded_prs(github, card, [pr], expected_qualifier=qualifier))
    return decisions


@dataclass(frozen=True)
class OrphanPrDecision:
    """One evaluated open PR and what the janitor did with it (FR-005)."""

    pr_number: int | None
    pr_url: str | None
    card_item_id: str | None
    basis: Literal["session_branch", "body_reference", "unresolved"]
    outcome: Literal[
        "closed", "skipped-active", "skipped-unresolved", "disabled", "skipped-comment-failed",
    ]
    comment_posted: bool = False
    closed_ok: bool = False


def _log_decision(decision: OrphanPrDecision) -> OrphanPrDecision:
    logger.info(
        "orphan_pr.decision",
        pr_number=decision.pr_number,
        pr_url=decision.pr_url,
        card_id=decision.card_item_id,
        basis=decision.basis,
        outcome=decision.outcome,
        comment_posted=decision.comment_posted,
        closed_ok=decision.closed_ok,
    )
    return decision


def _decision(
    pr: dict[str, Any],
    card_item_id: str | None,
    basis: Literal["session_branch", "body_reference", "unresolved"],
    outcome: Literal[
        "closed", "skipped-active", "skipped-unresolved", "disabled", "skipped-comment-failed",
    ],
) -> OrphanPrDecision:
    return OrphanPrDecision(
        pr_number=pr.get("number"),
        pr_url=pr.get("url"),
        card_item_id=card_item_id,
        basis=basis,
        outcome=outcome,
    )


async def close_superseded_prs(
    github: GitHubServiceProtocol,
    card: dict[str, Any],
    open_prs: list[dict[str, Any]],
    expected_qualifier: str | None = None,
) -> list[OrphanPrDecision]:
    """Close every open PR attributable to a completed *card*.

    US-1 completion-time path, also reused per-PR by the US-2 sweep.
    ``open_prs`` is the ``list_open_pull_requests`` snapshot taken by the
    caller; a merged winner is never in it (it is not open), so every
    attributable open PR here is superseded work. The card's terminal state
    is re-checked so a stale snapshot cannot close work that reopened between
    the board read and the close. A failed explanatory comment defers the
    close to the next cycle (FR-008: no bare closes).
    """
    card_id = str(card.get("id", ""))
    board_cards: dict[str, dict[str, Any]] = {card_id: card} if card_id else {}
    decisions: list[OrphanPrDecision] = []
    for pr in open_prs:
        attribution = resolve_pr_to_card(pr, board_cards, expected_qualifier=expected_qualifier)
        if attribution.card_item_id is None:
            decisions.append(_log_decision(_decision(pr, None, attribution.method, "skipped-unresolved")))
            continue
        if not is_terminal_card_status(card.get("status")):
            decisions.append(_log_decision(_decision(pr, card_id, attribution.method, "skipped-active")))
            continue
        # The board's pr_url can be an OPEN cross-reference (poll_board prefers
        # open over merged), i.e. this very PR — naming it as the superseding
        # winner would be false. Only name a URL that is not this PR's.
        comment_card = card
        if pr.get("url") and pr.get("url") == card.get("pr_url"):
            comment_card = {**card, "pr_url": None}
        comment = build_close_comment(comment_card, attribution.method)
        comment_posted = True
        try:
            await github.add_comment(pr["id"], comment)
        except Exception as exc:
            logger.warning(
                "orphan_pr.comment_failed", pr_number=pr.get("number"), error=str(exc),
            )
            decisions.append(
                _log_decision(
                    replace(
                        _decision(pr, card_id, attribution.method, "skipped-comment-failed"),
                        comment_posted=False,
                        closed_ok=False,
                    ),
                ),
            )
            continue
        result = await github.close_pull_request(pr["id"])
        closed_ok = bool(result.get("closed", False))
        if not closed_ok:
            logger.warning("orphan_pr.close_failed", pr_number=pr.get("number"))
        decisions.append(
            _log_decision(
                replace(
                    _decision(pr, card_id, attribution.method, "closed"),
                    comment_posted=comment_posted,
                    closed_ok=closed_ok,
                ),
            ),
        )
    return decisions


async def reconcile_completed_card(
    github: GitHubServiceProtocol,
    config: Any,
    card: dict[str, Any],
) -> list[OrphanPrDecision]:
    """Merge-path hook (US-1): close open PRs still pointing at *card*.

    Called from ``merge_pr`` right after the winning merge and card move to
    DONE. Gated on ``orphan_pr.enabled and orphan_pr.completion_reconciliation``
    — the disabled path returns without any GitHub read (SC-003).
    """
    orphan_cfg = getattr(config, "orphan_pr", None)
    if orphan_cfg is None or not (
        orphan_cfg.enabled and orphan_cfg.completion_reconciliation
    ):
        return []
    owner = getattr(github, "_org", None) or getattr(github, "org", None)
    repo = getattr(github, "_project_name", None) or getattr(github, "project_name", None)
    if not owner or not repo:
        logger.warning("orphan_pr.completion_no_repo_context")
        return []
    open_prs = await github.list_open_pull_requests(owner, repo)
    return await close_superseded_prs(
        github, card, open_prs, expected_qualifier=f"{owner}/{repo}",
    )

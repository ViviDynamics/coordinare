"""Branch-divergence detection for in-flight PR branches (047/096/097, 555).

Coordinare is an orchestrator: it never clones, rebases or pushes a branch.
This module only *decides*. It classifies a branch from the hosting
platform's mergeability verdict, applies the 096 anti-thrash marker, and on
divergence routes the card to the implementer with a relay-feedback
instruction (555). The implementer brings the branch up to date inside its
own container, where git credentials already exist, and publishes it with a
force-with-lease pinned to the head the daemon classified.

No git command runs here: the base branch head is read through the platform
API (``fetch_main_sha``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from coordinare.models.rebase import RebaseOutcome
from coordinare.services.closed_pr import is_closed_pr_block

if TYPE_CHECKING:
    from collections.abc import Mapping


logger = structlog.get_logger(__name__)

# Branch prefix for coordinare-managed branches.
_COORDINARE_BRANCH_PREFIX = "coordinare/"


# Outcomes that mean "nothing moved": do not re-issue against the same
# (main, head). BLOCKED/FAILED are pre-555 values still found in snapshots.
_NON_PROGRESSING_OUTCOMES = frozenset({
    RebaseOutcome.DELEGATED.value, RebaseOutcome.BLOCKED.value, RebaseOutcome.FAILED.value,
})


async def fetch_main_sha(github: Any, *, default_branch: str = "main") -> str | None:
    """Current head SHA of the default branch, read through the platform API.

    555: no git here. Returns None when the service cannot answer (no such
    method, transport error, malformed reply); every caller treats None as
    "unknown" and carries on.
    """
    reader = getattr(github, "get_branch_head_sha", None)
    if reader is None:
        return None
    try:
        sha = await reader(default_branch)
    except Exception as exc:
        logger.warning("fetch_main_sha.failed", error=type(exc).__name__)
        return None
    return sha if isinstance(sha, str) and sha else None


def _board_pauses_rebase(
    card_id: str, session: dict[str, Any], board_snapshot: Mapping[str, Any] | None,
) -> bool:
    """Durable pause and valid fresh board stop columns forbid divergence requests."""
    card = session.get("current_card") or {}
    identifiers = {card_id, str(card.get("id") or ""), str(card.get("content_id") or "")}
    board = board_snapshot or {}
    return bool(session.get("board_paused")) or any(
        isinstance(board.get(column), list) and identifier in board[column]
        for column in ("BACKLOG", "TODO", "DONE", "BLOCKED") for identifier in identifiers if identifier
    )


def detect_stale_branches(
    active_sessions: dict[str, Any],
    main_sha: str,
    *, board_snapshot: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Identify active sessions whose branches are candidates for a divergence check.

    Returns a list of dicts: ``{card_id, branch, pr_number, phase}``.
    Skips sessions where the performer is actively running (FR-006).
    """
    stale: list[dict[str, Any]] = []
    for card_id, session in active_sessions.items():
        if not isinstance(session, dict) or _board_pauses_rebase(card_id, session, board_snapshot):
            continue
        if is_closed_pr_block(session.get("system_error_reason")):
            continue
        branch = session.get("workspace_branch") or ""
        if not branch.startswith(_COORDINARE_BRANCH_PREFIX):
            continue
        # FR-006: skip branches with active performers — include them
        # in the result with skipped=True so the Slack summary and
        # dashboard show "N skipped (active performer)" instead of
        # silently omitting them.
        phase = session.get("phase", "")
        if phase == "monitoring_performer":
            logger.info(
                "rebase.skipped_active_performer",
                card_id=card_id,
                branch=branch,
            )
            stale.append({
                "card_id": card_id,
                "branch": branch,
                "pr_number": 0,
                "phase": phase,
                "skipped": True,
            })
            continue
        card = session.get("current_card") or {}
        pr_url = card.get("pr_url") or ""
        pr_number = 0
        if pr_url and "/" in pr_url:
            import contextlib
            with contextlib.suppress(ValueError, IndexError):
                pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        # Only rebase if the card has an open PR
        if not pr_url:
            continue
        stale.append({
            "card_id": card_id,
            "branch": branch,
            "pr_number": pr_number,
            "phase": phase,
        })
    return stale


def should_attempt_rebase(
    session: Mapping[str, Any], current_main_sha: str, head_sha: str,
) -> bool:
    """096 (FR-007), 555 (FR-007): anti-thrash guard for divergence requests.

    Returns ``False`` only when this card's most recent attempt hit a
    NON-progressing outcome (``DELEGATED``, or a pre-555 ``BLOCKED``/``FAILED``)
    against the SAME ``(main_sha, head_sha)`` — so the implementer is not handed
    the same divergence every cycle. Any change to the branch head (the performer
    pushed work) or the target main re-enables the attempt, as does a prior
    progressing outcome (``CLEAN``/``PERFORMER_RESOLVED``/``SKIPPED``) or no prior
    attempt at all.
    """
    prior = session.get("last_rebase_attempt")
    if not isinstance(prior, dict):
        return True
    if prior.get("outcome") not in _NON_PROGRESSING_OUTCOMES:
        return True
    return not (
        prior.get("main_sha") == current_main_sha and prior.get("head_sha") == head_sha
    )


def classify_pre_dispatch(
    mergeable_raw: str,
    merge_state_status: str,
    head_sha: str,
    session: Mapping[str, Any],
    current_main_sha: str,
) -> str:
    """097/555: decide what to do with an in-flight branch at the dispatch point.

    Pure (no I/O). Maps the platform mergeability + spec-096 anti-thrash marker to
    one of:
    - ``"unknown"``       — mergeability not yet computed (UNKNOWN / empty head);
      the caller dispatches normally (555 FR-005, fail open).
    - ``"proceed"``       — branch is current with main; dispatch unchanged.
    - ``"request"``       — branch is CONFLICTING or BEHIND and the implementer has
      not yet been asked about this (main, head): route it to the implementer.
    - ``"blocked_thrash"``— CONFLICTING/BEHIND but already requested against the
      same (main, head): do not ask again and do not dispatch onto the
      conflicting base (097 FR-008, 555 FR-007).
    """
    raw = (mergeable_raw or "").upper()
    mss = (merge_state_status or "").upper()
    if not head_sha or raw in ("", "UNKNOWN"):
        return "unknown"
    if raw == "CONFLICTING" or mss == "BEHIND":
        if should_attempt_rebase(session, current_main_sha, head_sha):
            return "request"
        return "blocked_thrash"
    return "proceed"


async def _live_pr_allows_rebase(session: Any, github: Any, card_id: str) -> bool:
    """Confirm PR lifecycle before a divergence request; an unreadable state defers."""
    card = session.get("current_card") or {}
    pr_id = str(card.get("pr_node_id") or "")
    live_state = ""
    try:
        if pr_id and github is not None:
            context = await github.get_pr_review_context(pr_id)
            if isinstance(context, dict):
                live_state = str(context.get("state") or "").upper()
    except Exception as exc:
        logger.warning("rebase.pr_state_unreadable", card_id=card_id, error_type=type(exc).__name__)
    allowed = live_state == "OPEN"
    if not allowed:
        logger.info("rebase.pr_state_deferred", card_id=card_id, pr_state=live_state or "UNKNOWN")
    return allowed


# relay_feedback ``kind`` the performer keys its rebase + lease push on (555).
DIVERGENCE_FEEDBACK_KIND = "branch_divergence"


def implementer_rebase_enabled(config: object) -> bool:
    """555 (FR-008): on by default; only an explicit ``enabled: false`` turns it off.

    A config object with no ``implementer_rebase`` attribute (test doubles, a
    partially built config) reads as the model default.
    """
    block = getattr(config, "implementer_rebase", None)
    return bool(getattr(block, "enabled", True)) if config is not None else False


def side_writer_active(session: Any) -> bool:
    """165: a documenter side run may be committing to this branch right now.

    A history rewrite under it would strand its push, so no rebase is requested
    until it has finished.
    """
    side = session.get("documenting_side")
    return isinstance(side, dict) and (side.get("status") == "running" or bool(side.get("writer_active")))


def build_divergence_feedback(
    branch: str, main_sha: str, head_sha: str, *, conflicting: bool, default_branch: str = "main",
) -> dict[str, str]:
    """Build the relay_feedback entry that hands a diverged branch to the implementer.

    The machine-readable keys are the performer's contract: it rebases the
    workspace onto ``base_branch`` before the agent starts and, at push time,
    publishes the rewritten branch with a force-with-lease pinned to
    ``expected_head_sha``. The body is what the agent reads when the rebase
    stopped on a conflict.
    """
    verdict = (
        f"conflicts with `{default_branch}`" if conflicting
        else f"is behind `{default_branch}`"
    )
    return {
        "kind": DIVERGENCE_FEEDBACK_KIND,
        "source": "coordinare",
        "base_branch": default_branch,
        "main_sha": main_sha,
        "expected_head_sha": head_sha,
        "body": (
            f"**Branch out of date with {default_branch}**: the PR branch `{branch}` {verdict} "
            f"(`{main_sha[:8]}`). It must be rebased onto `{default_branch}` in this turn. "
            f"Your workspace already attempted `git rebase origin/{default_branch}` when it "
            "was prepared. Before anything else:\n\n"
            f"1. Run `git status` and `git log --oneline -1 origin/{default_branch}`. If the "
            f"branch already sits on top of `origin/{default_branch}`, this item is done.\n"
            f"2. Otherwise run `git rebase origin/{default_branch}`. For each stop, resolve "
            "every conflict keeping the intent of both sides, remove all conflict markers, "
            "`git add` the files and `git rebase --continue`.\n"
            "3. Run the project's tests and checks on the rebased branch and commit any fix.\n\n"
            "Leave no rebase in progress. Do not push: the performer publishes the rebased "
            "branch itself with a lease, and refuses if anyone else pushed in the meantime. "
            "Then continue with any other work this turn carries."
        ),
    }


def request_implementer_rebase(
    session: Any,
    *,
    card_id: str,
    branch: str,
    main_sha: str,
    head_sha: str,
    conflicting: bool,
    reason: str,
    prev_main_sha: str | None = None,
) -> None:
    """555: route a diverged card to the implementer; the daemon touches no code.

    Queues the divergence instruction ahead of any feedback already waiting,
    points the session at the implementer stage via the existing dispatch
    pipeline, and stamps the 096 anti-thrash marker so the same
    ``(main_sha, head_sha)`` is never handed out twice (FR-007).

    Only mutates the session dict. If the implementer cannot resolve the
    divergence, the normal monitor_performer error/blocked path escalates.
    """
    entry = build_divergence_feedback(branch, main_sha, head_sha, conflicting=conflicting)
    queued = [item for item in (session.get("relay_feedback") or []) if item != entry]
    session["relay_feedback"] = [entry, *queued]
    session["performer_stage"] = "implementing"
    session["phase"] = "dispatching"
    session["agent_dispatch"] = {}
    session["agent_dispatch_at"] = None
    session["last_rebase_attempt"] = {
        "main_sha": main_sha,
        "head_sha": head_sha,
        "outcome": RebaseOutcome.DELEGATED.value,
    }
    logger.info(
        "rebase.triggered", reason=reason,
        card_id=card_id, branch=branch,
        prev_main_sha=prev_main_sha, current_main_sha=main_sha,
        outcome=RebaseOutcome.DELEGATED.value,
    )
    logger.info(
        "rebase.performer_dispatched",
        card_id=card_id, branch=branch, conflicting=conflicting,
    )

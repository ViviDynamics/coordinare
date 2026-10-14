"""Why a To Do card has no session (#149).

``check_board`` is the one place the conductor decides whether a To Do card is
picked up. Three things keep a card out: the advocate escalated it to a
person, the ownership filter does not admit it, or a dependency has not
reached DONE. When none of those applies and the pipeline budget does not
admit a new card, the card is queued. The budget verdict is the one the
dispatch gate uses (``pipeline_budget``): the daemon's selected set, and the
review cap. The helpers here write each decision into the per-symphony state
where it is made, so the dashboard names it instead of re-deriving it.

A card with a session never gets an entry here; its wait is the session's
``session_skip_reasons`` entry, written by the daemon.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from coordinare.models.dependency import DependencyStatus
from coordinare.services.card_ownership import owns_card
from coordinare.services.dependency import build_graph
from coordinare.services.pipeline_budget import (
    configured_review_limit,
    parked_in_review,
    review_cap_allows_new_card,
    review_cap_detail,
    select_pipelines,
)

if TYPE_CHECKING:
    from coordinare.models.dependency import DependencyGraph
    from coordinare.services.card_ownership import OwnershipPolicy

logger = structlog.get_logger(__name__)

REASON_QUEUED = "queued"
REASON_REVIEW_CAPACITY = "review_capacity"
REASON_WAITING_ON = "waiting_on"
REASON_ESCALATED = "escalated"
REASON_ASSIGNED_ELSEWHERE = "assigned_elsewhere"
REASON_UNASSIGNED = "unassigned"


def _pending_blockers(dep_graph: DependencyGraph, card_id: str) -> list[str]:
    """Blockers still on the board and not done, as display labels.

    An unresolvable blocker is left out on purpose: the conductor does not
    wait on it, it moves the card to Blocked with a comment instead, so naming
    it here would be a guess about what happens next.
    """
    return [
        f"{dep.blocker_repo}#{dep.blocker_issue_number}" if dep.blocker_repo else f"#{dep.blocker_issue_number}"
        for dep in dep_graph.by_dependent.get(card_id, [])
        if dep.status == DependencyStatus.PENDING
    ]


def _dependency_graph(state: Any, board: dict[str, Any]) -> DependencyGraph:
    config = state.get("config")
    org = getattr(config, "github_org", "") if config is not None else ""
    project = getattr(config, "project_name", "") if config is not None else ""
    repo_slug = f"{org}/{project}" if org and project else ""
    return build_graph(board, repo=repo_slug)


def _dependency_graph_or_none(state: Any, board: dict[str, Any]) -> DependencyGraph | None:
    """A failed graph build leaves the queue reason blank, never a guessed one."""
    try:
        return _dependency_graph(state, board)
    except Exception:
        logger.warning("todo_waits.dependency_graph_failed", exc_info=True)
        return None


def _budget_hold(
    state: Any, sessions: dict[str, Any], limit: int, review_limit: int | None,
) -> tuple[str | None, int]:
    """Why a new card is not admitted right now, and the slots in use.

    The same two tests the dispatch gate applies, in the same order: a full
    set of slots queues the card; with slots free, a review cap that is reached
    holds it back. ``None`` means a new card would be admitted.
    """
    selected = state.get("_pipeline_selected")
    if selected is None:
        # Single-card callers have no daemon-selected set; compute it on copies
        # so select_pipelines' bookkeeping cannot touch the live sessions.
        selected = select_pipelines({cid: dict(s) for cid, s in sessions.items()}, limit, review_limit=review_limit)
    in_use = len(selected)
    if in_use >= limit:
        return REASON_QUEUED, in_use
    if not review_cap_allows_new_card(sessions, review_limit):
        return REASON_REVIEW_CAPACITY, in_use
    return None, in_use


def _hold_entry(hold: str, waiting: int, review_limit: int | None) -> dict[str, Any]:
    detail = review_cap_detail(review_limit, waiting) if hold == REASON_REVIEW_CAPACITY else None
    return {"reason": hold, "blockers": [], "detail": detail}


def refresh_todo_waits(
    state: Any,
    *,
    board: dict[str, Any],
    todo: list[str],
    active_card_ids: set[str],
    ownership: OwnershipPolicy,
    assignees: dict[str, Any],
    limit: int,
) -> None:
    """Record the wait for every To Do card, before any pickup is attempted.

    Escalation and ownership are the same tests the pickup applies, in the
    same order. Dependencies are read from the cached board only, which is
    enough when the budget holds the card: it could not be admitted either way,
    so the only question left is which reason to show. When the budget admits a
    new card, the card's dependency verdict comes from the dependency filter
    itself (``record_dependency_waits``) and this step records nothing for it.
    """
    sessions = state.get("active_sessions") or {}
    review_limit = configured_review_limit(state.get("config"))
    hold, in_use = _budget_hold(state, sessions, limit, review_limit)
    waiting = parked_in_review(sessions)
    labels = board.get("item_labels", {}) if isinstance(board, dict) else {}
    escalation = str(state.get("advocate_escalation_label", "") or "")
    dep_graph = _dependency_graph_or_none(state, board) if hold is not None else None
    waits: dict[str, dict[str, Any]] = {}
    for card_id in todo:
        if card_id in active_card_ids:
            continue
        if escalation and escalation in (labels.get(card_id) or []):
            waits[card_id] = {"reason": REASON_ESCALATED, "blockers": [], "detail": None}
        elif not owns_card(ownership, card_id, assignees):
            reason = REASON_ASSIGNED_ELSEWHERE if assignees.get(card_id) else REASON_UNASSIGNED
            waits[card_id] = {"reason": reason, "blockers": [], "detail": None}
        elif hold is not None and dep_graph is not None:
            blockers = _pending_blockers(dep_graph, card_id)
            waits[card_id] = (
                {"reason": REASON_WAITING_ON, "blockers": blockers, "detail": None}
                if blockers else _hold_entry(hold, waiting, review_limit)
            )
    state["todo_waits"] = waits
    state["todo_capacity"] = {
        "in_use": in_use,
        "limit": limit,
        "waiting_on_review": waiting,
        "review_limit": review_limit,
        "hold": hold,
    }


def record_dependency_waits(
    state: Any,
    *,
    dep_graph: DependencyGraph,
    filtered_ids: list[str],
    passed_ids: list[str],
) -> None:
    """Write the dependency filter's verdict over the pre-pickup estimate.

    The filter has read comments and resolved off-board blockers by the time it
    runs, so its verdict replaces whatever the estimate said. A card it held
    back is waiting on its pending blockers, or shows no reason if the only
    blocker is unresolvable. A card it let through takes the budget's hold
    (queued or review capacity) when there is one, and no reason when a slot is
    free.
    """
    waits = dict(state.get("todo_waits") or {})
    capacity = state.get("todo_capacity") or {}
    hold = capacity.get("hold")
    for card_id in filtered_ids:
        blockers = _pending_blockers(dep_graph, card_id)
        if blockers:
            waits[card_id] = {"reason": REASON_WAITING_ON, "blockers": blockers, "detail": None}
        else:
            waits.pop(card_id, None)
    for card_id in passed_ids:
        if hold is not None:
            waits[card_id] = _hold_entry(hold, int(capacity.get("waiting_on_review", 0)), capacity.get("review_limit"))
        else:
            waits.pop(card_id, None)
    state["todo_waits"] = waits

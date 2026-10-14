"""Why a To Do or Blocked card has no session (#149).

Each entry reports the decision that actually holds the card. For a card with
no session there are three such decisions, all made in ``check_board``:

- escalated, assigned_elsewhere, unassigned: the pickup's own escalation and
  ownership tests, applied in the same order.
- waiting_on: a dependency that has not reached DONE. The dependency filter
  (``record_dependency_waits``) is the real verdict. Until it runs in a cycle,
  ``refresh_todo_waits`` estimates it from the cached board, and only
  PENDING blockers count there because an off-board closed issue is not known
  to be closed without the filter's IO.
- queued: the pickup admits no new card. ``open_slots`` is the one count
  ``_pickup_todo_cards`` uses, so the reason cannot disagree with the pickup.
  A card the pickup did not admit is recorded by ``record_pickup_outcome``.

The review cap (``max_cards_in_review``) and the dispatch gate decide only
cards that already have a session. Those carry ``session_skip_reasons`` from
the daemon, not an entry here. A To Do card with a free pickup slot is
admitted, and the review cap is then named on its new session.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from coordinare.models.dependency import DependencyStatus
from coordinare.services.card_ownership import owns_card
from coordinare.services.dependency import build_graph
from coordinare.services.pipeline_budget import configured_review_limit, parked_in_review

if TYPE_CHECKING:
    from coordinare.models.dependency import DependencyGraph
    from coordinare.services.card_ownership import OwnershipPolicy

logger = structlog.get_logger(__name__)

REASON_QUEUED = "queued"
REASON_WAITING_ON = "waiting_on"
REASON_ESCALATED = "escalated"
REASON_ASSIGNED_ELSEWHERE = "assigned_elsewhere"
REASON_UNASSIGNED = "unassigned"

VERDICT_ESTIMATE = "estimate"
VERDICT_DEPENDENCY = "dependency"
VERDICT_PICKUP = "pickup"

_warned_for_cycle: dict[str, object] = {}


def open_slots(in_use: int, limit: int) -> int:
    """How many new cards the pickup may admit. ``_pickup_todo_cards`` calls this too."""
    return max(0, limit - in_use)


def _label(dep: Any) -> str:
    return f"{dep.blocker_repo}#{dep.blocker_issue_number}" if dep.blocker_repo else f"#{dep.blocker_issue_number}"


def _blockers(dep_graph: DependencyGraph, card_id: str, statuses: frozenset[DependencyStatus]) -> list[str]:
    return [_label(dep) for dep in dep_graph.by_dependent.get(card_id, []) if dep.status in statuses]


def _dependency_graph(state: Any, board: dict[str, Any]) -> DependencyGraph:
    config = state.get("config")
    org = getattr(config, "github_org", "") if config is not None else ""
    project = getattr(config, "project_name", "") if config is not None else ""
    repo_slug = f"{org}/{project}" if org and project else ""
    return build_graph(board, repo=repo_slug)


def _dependency_graph_or_none(state: Any, board: dict[str, Any]) -> DependencyGraph | None:
    """A failed graph build leaves the dependency reason blank, never a guessed one."""
    try:
        return _dependency_graph(state, board)
    except Exception:
        logger.warning("todo_waits.dependency_graph_failed", exc_info=True)
        return None


def _entry(reason: str, blockers: list[str], verdict: str) -> dict[str, Any]:
    return {"reason": reason, "blockers": blockers, "verdict": verdict}


def clear_waits(state: Any) -> None:
    """Forget every verdict. Used when the board is unreadable or a recorder fails."""
    state["todo_waits"] = {}
    state["todo_capacity"] = None


def warn_once_per_cycle(state: Any, failure: str) -> None:
    """A display bug is logged once per cycle, not once per session tick."""
    token = state.get("orphan_sweep_cycle_token")
    if _warned_for_cycle.get("token") == token:
        return
    _warned_for_cycle["token"] = token
    logger.warning("check_board.todo_waits_failed", failure=failure, exc_info=True)


def refresh_todo_waits(
    state: Any,
    *,
    board: dict[str, Any],
    todo: list[str],
    blocked: list[str],
    active_card_ids: set[str],
    ownership: OwnershipPolicy,
    assignees: dict[str, Any],
    in_use: int,
    limit: int,
) -> None:
    """Record the estimated wait for every To Do card, and keep the real dependency
    verdicts already recorded for cards that have since moved to Blocked.

    ``in_use`` is the count ``_pickup_todo_cards`` will use, passed in by
    check_board so both read the same number.
    """
    sessions = state.get("active_sessions") or {}
    review_limit = configured_review_limit(state.get("config"))
    waiting = parked_in_review(sessions)
    labels = board.get("item_labels", {}) if isinstance(board, dict) else {}
    escalation = str(state.get("advocate_escalation_label", "") or "")
    prev = state.get("todo_waits") or {}
    blocked_set = set(blocked)
    waits: dict[str, dict[str, Any]] = {}
    for card_id in blocked:
        old = prev.get(card_id)
        if card_id not in active_card_ids and isinstance(old, dict) and old.get("verdict") == VERDICT_DEPENDENCY:
            waits[card_id] = old
    full = open_slots(in_use, limit) == 0
    dep_graph = _dependency_graph_or_none(state, board)
    for card_id in todo:
        if card_id in active_card_ids or card_id in blocked_set:
            continue
        if escalation and escalation in (labels.get(card_id) or []):
            waits[card_id] = _entry(REASON_ESCALATED, [], VERDICT_ESTIMATE)
        elif not owns_card(ownership, card_id, assignees):
            reason = REASON_ASSIGNED_ELSEWHERE if assignees.get(card_id) else REASON_UNASSIGNED
            waits[card_id] = _entry(reason, [], VERDICT_ESTIMATE)
        elif dep_graph is not None:
            open_blockers = _blockers(dep_graph, card_id, frozenset({DependencyStatus.PENDING}))
            if open_blockers:
                waits[card_id] = _entry(REASON_WAITING_ON, open_blockers, VERDICT_ESTIMATE)
            elif full:
                waits[card_id] = _entry(REASON_QUEUED, [], VERDICT_ESTIMATE)
    state["todo_waits"] = waits
    state["todo_capacity"] = {
        "in_use": in_use,
        "limit": limit,
        "waiting_on_review": waiting,
        "review_limit": review_limit,
        "hold": REASON_QUEUED if full else None,
    }


def record_dependency_waits(
    state: Any,
    *,
    dep_graph: DependencyGraph,
    filtered_ids: list[str],
    passed_ids: list[str],
) -> None:
    """Write the dependency filter's verdict, which replaces the estimate.

    The filter has read comments and resolved off-board blockers by now, so an
    UNRESOLVABLE blocker is a known wait here (the card is moved to Blocked
    with a comment). A card the filter lets through has no dependency wait; the
    pickup records its queued verdict.
    """
    waits = dict(state.get("todo_waits") or {})
    statuses = frozenset({DependencyStatus.PENDING, DependencyStatus.UNRESOLVABLE})
    for card_id in filtered_ids:
        blockers = _blockers(dep_graph, card_id, statuses)
        if blockers:
            waits[card_id] = _entry(REASON_WAITING_ON, blockers, VERDICT_DEPENDENCY)
        else:
            waits.pop(card_id, None)
    for card_id in passed_ids:
        waits.pop(card_id, None)
    state["todo_waits"] = waits


def record_pickup_outcome(
    state: Any,
    *,
    held: list[str],
    admitted: list[str],
    in_use: int,
    limit: int,
) -> None:
    """Record what ``_pickup_todo_cards`` decided: held cards are queued, admitted cards have a session."""
    waits = dict(state.get("todo_waits") or {})
    for card_id in admitted:
        waits.pop(card_id, None)
    for card_id in held:
        waits[card_id] = _entry(REASON_QUEUED, [], VERDICT_PICKUP)
    state["todo_waits"] = waits
    capacity = dict(state.get("todo_capacity") or {})
    capacity.update(in_use=in_use, limit=limit, hold=REASON_QUEUED if held else None)
    state["todo_capacity"] = capacity

"""Spec 076 T122 — reconcile_board_state unit tests.

Covers FR-025: per-cycle reconciliation between project-board Status
and local state.active_card.status.  Default action on divergence is
release-the-pin (consistent with clarification Q1).
"""
from __future__ import annotations

from coordinare.services.reconciliation import reconcile_board_state


def _state_with_active(card_id: str = "PVTI_X", local_status: str = "IN_PROGRESS") -> dict:
    return {
        "active_card": {"id": card_id, "status": local_status, "title": "card"},
        "current_card": {"id": card_id, "status": local_status, "title": "card"},
        "active_card_id": card_id,
    }


def test_no_active_card_returns_no_op() -> None:
    result = reconcile_board_state({"active_card": None}, {})
    assert result["action"] == "no_active_card"


def test_board_and_local_agree_returns_agreed() -> None:
    state = _state_with_active(local_status="IN_PROGRESS")
    result = reconcile_board_state(state, {"IN_PROGRESS": ["PVTI_X"]})
    assert result["action"] == "agreed"
    # State unchanged
    assert state["active_card"] is not None


def test_board_todo_local_in_progress_releases_pin() -> None:
    """Today's exact divergence: card moved back to TODO on the board
    but coordinare's active_card still pinned at IN_PROGRESS."""
    state = _state_with_active(local_status="IN_PROGRESS")
    result = reconcile_board_state(state, {"TODO": ["PVTI_X"]})
    assert result["action"] == "pin_released"
    assert result["board"] == "TODO"
    assert result["local"] == "IN_PROGRESS"
    # Pin released — next cycle's eligibility can re-pick
    assert state["active_card"] is None


def test_card_missing_from_board_releases_pin() -> None:
    """If the card is gone from every tracked column (deleted /
    archived), release the pin."""
    state = _state_with_active()
    result = reconcile_board_state(state, {
        "IN_PROGRESS": [],
        "TODO": [],
        "DONE": [],
    })
    assert result["action"] == "pin_released"
    assert result["board"] == "missing"
    assert state["active_card"] is None


def test_card_in_done_column_with_local_in_progress_deferred() -> None:
    """Card moved to DONE while local is IN_PROGRESS is a legitimate
    forward divergence — operator may have manually closed the card.
    The existing startup reconciliation handles this; the per-cycle
    check defers (logs, doesn't release).  Only the TODO/BACKLOG
    "backwards" divergence (today's wedge pattern) releases the pin."""
    state = _state_with_active(local_status="IN_PROGRESS")
    result = reconcile_board_state(state, {"DONE": ["PVTI_X"]})
    assert result["action"] == "deferred"
    assert state["active_card"] is not None  # pin retained


def test_card_in_backlog_with_local_in_progress_releases() -> None:
    """BACKLOG triggers the same release path as TODO — both are
    'card returned to the queue while we thought we owned it'."""
    state = _state_with_active(local_status="IN_PROGRESS")
    result = reconcile_board_state(state, {"BACKLOG": ["PVTI_X"]})
    assert result["action"] == "pin_released"


def test_card_with_no_id_is_skipped() -> None:
    state = {"active_card": {"status": "IN_PROGRESS"}, "current_card": None}
    result = reconcile_board_state(state, {})
    assert result["action"] == "no_active_card"

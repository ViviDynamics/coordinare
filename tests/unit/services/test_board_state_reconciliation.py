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
        "active_sessions": {card_id: {"performer_stage": "implementing"}},
        "agent_dispatch": {"session_id": "s1"},
        "agent_dispatch_at": "2026-10-03T00:00:00Z",
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


def test_board_done_with_local_in_progress_retires_session() -> None:
    """Issue #516: card reached DONE on the board (PR merged) while the
    local pin stayed IN_PROGRESS and the session record survived. The
    per-cycle reconciliation must retire the session and free the slot;
    deferring wedges the pickup lane forever."""
    state = _state_with_active(local_status="IN_PROGRESS")
    result = reconcile_board_state(state, {"DONE": ["PVTI_X"]})
    assert result["action"] == "done_session_retired"
    assert result["board"] == "DONE"
    assert result["local"] == "IN_PROGRESS"
    # The retiring session is handed back so the caller can best-effort
    # release its performer resources before the record is dropped.
    assert result["retired_session"] == {"performer_stage": "implementing"}
    assert state["active_sessions"] == {}
    assert state["active_card_id"] is None
    assert state["active_card"] is None
    assert state["current_card"] is None
    assert state["agent_dispatch"] == {}
    assert state["agent_dispatch_at"] is None
    assert state["phase"] == "idle"


def test_board_done_with_local_done_retires_session() -> None:
    """A restart between the status write and the session teardown leaves
    local=DONE + board=DONE + a live session. That is still a wedge: the
    agreed-status short-circuit must not shadow the terminal case."""
    state = _state_with_active(local_status="DONE")
    result = reconcile_board_state(state, {"DONE": ["PVTI_X"]})
    assert result["action"] == "done_session_retired"
    assert result["retired_session"] == {"performer_stage": "implementing"}
    assert state["active_sessions"] == {}
    assert state["active_card_id"] is None


def test_board_done_without_session_still_clears_mirrors() -> None:
    """Retirement is idempotent: no session record left (e.g. startup
    already swept it) still clears the derived mirror and reports the
    same action."""
    state = _state_with_active()
    state["active_sessions"] = {}
    state["agent_dispatch"] = {"session_id": "s1"}
    result = reconcile_board_state(state, {"DONE": ["PVTI_X"]})
    assert result["action"] == "done_session_retired"
    assert state["active_card"] is None
    assert state["agent_dispatch"] == {}
    assert not result.get("retired_session")


def test_card_in_review_with_local_in_progress_deferred() -> None:
    """Forward divergence to IN_REVIEW stays deferred — the card is
    legitimately advancing and the monitor owns that transition."""
    state = _state_with_active(local_status="IN_PROGRESS")
    result = reconcile_board_state(state, {"IN_REVIEW": ["PVTI_X"]})
    assert result["action"] == "deferred"
    assert state["active_sessions"] == {"PVTI_X": {"performer_stage": "implementing"}}


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

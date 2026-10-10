from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from coordinare.graph.nodes.check_board import _finalize_active_card, _handle_blocked_cards
from coordinare.services.board_provider import MoveOutcome
from coordinare.session import create_session_from_card, session_to_state, state_to_session


class ReplyBoard:
    def __init__(self, *, has_reply: bool = True) -> None:
        self.has_reply = has_reply
        self.reads: list[str] = []
        self.moves: list[tuple[str, str]] = []

    async def get_card(self, card_id: str) -> dict[str, Any]:
        self.reads.append(card_id)
        comments = [{
            "body": f"Answer for {card_id}",
            "author": {"login": "human"},
            "createdAt": datetime.now(UTC).isoformat(),
        }] if self.has_reply else []
        return {"comments": {"nodes": comments}}

    async def move_card(self, card_id: str, status: str) -> MoveOutcome:
        self.moves.append((card_id, status))
        return MoveOutcome.ok()


def board_data() -> dict[str, Any]:
    return {
        "titles": {"A": "Task A", "B": "Task B"},
        "descriptions": {"A": "Independent A", "B": "Independent B"},
        "issue_numbers": {"A": 1, "B": 2},
        "content_node_ids": {"A": "ISSUE_A", "B": "ISSUE_B"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("old_focus", [None, "A"])
async def test_new_blocked_card_cannot_inherit_retired_card_context(old_focus: str | None) -> None:
    old = create_session_from_card({"id": "A", "status": "DONE"})
    old["phase"] = "idle"
    old["card_clarifications"] = [{"body": "Only A preserves tabs", "author": "human"}]
    old["open_questions"] = ["Only A question"]
    state: dict[str, Any] = {
        "active_sessions": {"A": old}, "active_card_id": old_focus, "current_card": None,
        "phase": "blocked", "last_blocked_notified_at": None,
        "card_clarifications": list(old["card_clarifications"]), "open_questions": ["Only A question"],
        "dispatched_feedback": {"reviews": [{"body": "Only A feedback"}]},
    }
    await _handle_blocked_cards(state, ReplyBoard(), board_data(), ["B"], [])
    _finalize_active_card(state)
    saved = state_to_session(state)
    assert saved["current_card"]["id"] == "B"
    assert saved["card_clarifications"] == []
    assert saved["open_questions"] == []
    assert saved["dispatched_feedback"] == {}
    assert old["open_questions"] == ["Only A question"]
    assert old["card_clarifications"][0]["body"] == "Only A preserves tabs"


def focused_b() -> dict[str, Any]:
    a = create_session_from_card({"id": "A", "status": "BLOCKED"})
    b = create_session_from_card({"id": "B", "status": "BLOCKED"})
    a["phase"] = b["phase"] = "blocked"
    b["open_questions"] = ["Choose B behavior"]
    b["card_clarifications"] = [{"body": "Existing B context", "author": "human"}]
    b["last_blocked_notified_at"] = datetime.now(UTC) - timedelta(seconds=60)
    b["dispatched_feedback"] = {"reviews": [{"body": "Existing B PR request"}]}
    state: dict[str, Any] = {"active_sessions": {"A": a, "B": b}, "active_card_id": "B"}
    session_to_state(b, state)
    return state


@pytest.mark.asyncio
async def test_focused_blocked_card_collects_its_own_reply_not_first_siblings() -> None:
    state = focused_b()
    provider = ReplyBoard()
    await _handle_blocked_cards(state, provider, board_data(), ["A", "B"], [])
    _finalize_active_card(state)
    saved = state_to_session(state)
    assert provider.reads == ["ISSUE_B"]
    assert provider.moves == [("B", "IN_PROGRESS")]
    assert saved["current_card"]["id"] == "B"
    assert saved["card_clarifications"][-1] == {"questions": ["Choose B behavior"], "answer": "Answer for ISSUE_B"}
    assert saved["dispatched_feedback"] == {"reviews": [{"body": "Existing B PR request"}]}
    assert state["active_sessions"]["A"]["current_card"]["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_no_new_reply_preserves_focused_cards_fresh_questions_and_history() -> None:
    state = focused_b()
    state["open_questions"] = ["Fresh unsaved B question"]
    provider = ReplyBoard(has_reply=False)
    await _handle_blocked_cards(state, provider, board_data(), ["A", "B"], [])
    _finalize_active_card(state)
    saved = state_to_session(state)
    assert provider.reads == ["ISSUE_B"]
    assert saved["open_questions"] == ["Fresh unsaved B question"]
    assert saved["card_clarifications"] == [{"body": "Existing B context", "author": "human"}]
    assert saved["dispatched_feedback"] == {"reviews": [{"body": "Existing B PR request"}]}
    assert saved["phase"] == "blocked"


@pytest.mark.asyncio
async def test_existing_blocked_card_hydrates_its_own_context_when_no_card_is_focused() -> None:
    state = focused_b()
    state.update({"current_card": None, "active_card_id": None,
                  "card_clarifications": [{"body": "Foreign A context", "author": "human"}],
                  "open_questions": ["Foreign A question"]})
    provider = ReplyBoard(has_reply=False)
    await _handle_blocked_cards(state, provider, board_data(), ["B"], [])
    _finalize_active_card(state)
    saved = state_to_session(state)
    assert saved["current_card"]["id"] == "B"
    assert saved["card_clarifications"] == [{"body": "Existing B context", "author": "human"}]
    assert saved["open_questions"] == ["Choose B behavior"]
    assert saved["dispatched_feedback"] == {"reviews": [{"body": "Existing B PR request"}]}

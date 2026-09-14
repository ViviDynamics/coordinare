"""410: a card the assessor declines leaves the board in a decided state.

not_work closes the issue, moves the project item to DONE and records the
reasoning in a comment; needs_split moves the card back to the backlog with
the proposed split. Both retire the run and both survive a board side that is
missing or failing.
"""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.dispatch_performer import _card_labels
from coordinare.graph.nodes.monitor_performer import _apply_assessor_decline
from coordinare.services.board_provider import MoveOutcome


def _state() -> dict:
    return {
        "config": None,
        "active_card_id": "CARD-1",
        "current_card": "CARD-1",
        "active_sessions": {"CARD-1": {"performer_stage": "assessing"}},
        "agent_dispatch": {"x": 1},
        "agent_dispatch_at": "t",
        "relay_feedback": ["f"],
        "pending_reviews": ["p"],
        "open_questions": ["q"],
        "performer_stage": "assessing",
        "lifecycle_sequence": ["implementing", "assessing"],
        "system_error_count": 3,
        "system_error_reason": "r",
    }


def _card() -> dict:
    return {"id": "CARD-1", "issue_id": "node-1"}


def _status() -> dict:
    return {
        "report": {
            "assessment": {
                "expected_behavior": "the premise is wrong",
                "goal": "g",
            }
        }
    }


class _FakeGitHub:
    def __init__(self, fail_close: bool = False):
        self.comments: list[str] = []
        self.closed: list[str] = []
        self._fail_close = fail_close

    async def add_comment(self, issue_id: str, body: str) -> None:
        self.comments.append(body)

    async def close_issue(self, issue_id: str) -> None:
        if self._fail_close:
            raise RuntimeError("graphql down")
        self.closed.append(issue_id)


class _FakeBoard:
    def __init__(self):
        self.moved: list[tuple[str, str]] = []

    async def move_card(self, card_id: str, column: str) -> MoveOutcome:
        self.moved.append((card_id, column))
        return MoveOutcome.ok()


@pytest.mark.asyncio
async def test_not_work_comments_and_closes():
    state = _state()
    gh = _FakeGitHub()
    board = _FakeBoard()
    out = await _apply_assessor_decline(
        state, _card(), "CARD-1", "assessment_not_work", _status(), board, gh
    )
    assert len(gh.comments) == 1
    assert "not work" in gh.comments[0]
    assert gh.closed == ["node-1"], "not_work closes the issue"
    assert board.moved == [("CARD-1", "DONE")], "the project item leaves TODO or the next poll re-admits the card"
    assert out["phase"] == "idle"
    assert out["agent_dispatch"] == {}
    assert out["active_card_id"] is None
    assert "CARD-1" not in (out["active_sessions"] or {})


@pytest.mark.asyncio
async def test_needs_split_moves_backlog_and_does_not_close():
    state = _state()
    gh = _FakeGitHub()
    board = _FakeBoard()
    out = await _apply_assessor_decline(
        state, _card(), "CARD-1", "assessment_needs_split", _status(), board, gh
    )
    assert len(gh.comments) == 1
    assert "split" in gh.comments[0].lower()
    assert gh.closed == [], "needs_split keeps the issue open"
    assert board.moved == [("CARD-1", "BACKLOG")]
    assert out["phase"] == "idle"


@pytest.mark.asyncio
async def test_missing_github_still_retires():
    state = _state()
    out = await _apply_assessor_decline(
        state, _card(), "CARD-1", "assessment_not_work", _status(), None, None
    )
    assert out["phase"] == "idle"
    assert out["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_board_failure_still_retires():
    state = _state()
    gh = _FakeGitHub(fail_close=True)
    out = await _apply_assessor_decline(
        state, _card(), "CARD-1", "assessment_not_work", _status(), None, gh
    )
    assert gh.comments, "the comment may still have landed"
    assert out["phase"] == "idle", "the run's retirement must not depend on a mutation"


# --- the labels ride the dispatch payload ---------------------------------------

def test_labels_come_from_the_board_snapshot():
    state = {"_board_item_labels": {"CARD-1": ["documentation", "question"]}}
    assert _card_labels(state, "CARD-1") == ["documentation", "question"]


def test_labels_empty_when_absent():
    assert _card_labels({"_board_item_labels": {}}, "CARD-1") == []
    assert _card_labels({}, "CARD-1") == []


def test_labels_drop_blank_entries():
    state = {"_board_item_labels": {"CARD-1": ["docs", "  ", None]}}
    assert _card_labels(state, "CARD-1") == ["docs"]


def test_labels_non_list_is_ignored():
    state = {"_board_item_labels": {"CARD-1": "documentation"}}
    assert _card_labels(state, "CARD-1") == []

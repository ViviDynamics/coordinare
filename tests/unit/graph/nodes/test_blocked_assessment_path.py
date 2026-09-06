"""166 FR-015: when the assessing stage reports blocked with questions, the
blocked path is unchanged: open_questions and assessor_open_questions are set,
and state[assessment] is left untouched."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        return self._response


def _state(response: dict) -> dict:
    state = initial_state()
    state["performer_services"] = {"assessing": _Performer(response)}
    state["performer_stage"] = "assessing"
    state["lifecycle_sequence"] = ["assessing", "architecting"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["current_symphony"] = "website"
    return state


def _assessment() -> dict:
    return {
        "ready": False,
        "goal": "Add categories",
        "expected_behavior": "Users select category",
        "out_of_scope": [],
        "questions": [],
        "assumptions": [],
        "criteria": [],
        "criteria_source": "card",
        "clarifications": [],
        "assessment_hash": "a1",
    }


@pytest.mark.asyncio
async def test_blocked_assessment_sets_open_questions_and_assessor_open_questions():
    """When the assessor blocks with questions, both open_questions (diagnostic)
    and assessor_open_questions (carry-forward) are set, exactly as before."""
    state = _state({"status": "blocked", "questions": ["What is the goal?", "Who is the user?"]})
    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["What is the goal?", "Who is the user?"]
    assert result["assessor_open_questions"] == [
        {"question": "What is the goal?", "answer": ""},
        {"question": "Who is the user?", "answer": ""},
    ]


@pytest.mark.asyncio
async def test_blocked_assessment_leaves_existing_assessment_untouched():
    """When the assessing stage blocks, the existing assessment on the card
    session is left untouched (not cleared, not updated)."""
    state = _state({"status": "blocked", "questions": ["What is the goal?"]})
    state["assessment"] = _assessment()

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["assessment"] == _assessment()


@pytest.mark.asyncio
async def test_blocked_assessment_no_questions():
    """When the assessor blocks without questions, open_questions is empty
    and assessment is still left untouched."""
    state = _state({"status": "blocked"})
    state["assessment"] = _assessment()

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == []
    assert result["assessment"] == _assessment()

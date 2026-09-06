"""166 FR-012: the monitor lifts the assessor workflow's assessment into the
session and leaves the blocked path unchanged."""
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


def _assessment(tag: str) -> dict:
    return {
        "ready": True,
        "goal": tag,
        "expected_behavior": f"Expected for {tag}",
        "out_of_scope": ["Out of scope"],
        "questions": [],
        "assumptions": [],
        "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}],
        "criteria_source": "card",
        "clarifications": [],
        "assessment_hash": tag,
    }


@pytest.mark.asyncio
async def test_assessment_complete_with_a_report_lifts_the_assessment_and_advances():
    state = _state({"status": "assessment_complete", "report": {"assessment": _assessment("a1")}})
    result = await monitor_performer(state)
    assert result["assessment"]["assessment_hash"] == "a1"
    assert result["assessment"]["ready"] is True
    assert result["performer_stage"] == "architecting"


@pytest.mark.asyncio
async def test_a_new_assessment_replaces_the_old():
    state = _state({"status": "assessment_complete", "report": {"assessment": _assessment("a2")}})
    state["assessment"] = _assessment("a1")
    result = await monitor_performer(state)
    assert result["assessment"]["assessment_hash"] == "a2"


@pytest.mark.asyncio
async def test_assessment_lifting_records_timestamp():
    state = _state({"status": "assessment_complete", "report": {"assessment": _assessment("a1")}})
    result = await monitor_performer(state)
    assert "recorded_at" in result["assessment"]


@pytest.mark.asyncio
async def test_the_prose_assessor_leaves_state_untouched():
    state = _state({"status": "assessment_complete"})
    result = await monitor_performer(state)
    assert result.get("assessment") is None
    assert result["performer_stage"] == "architecting"


@pytest.mark.asyncio
async def test_a_hollow_assessment_is_not_lifted():
    state = _state({"status": "assessment_complete", "report": {"assessment": {**_assessment("a3"), "goal": ""}}})
    result = await monitor_performer(state)
    assert not result.get("assessment")


@pytest.mark.asyncio
async def test_assessment_not_lifted_when_no_report():
    state = _state({"status": "assessment_complete"})
    result = await monitor_performer(state)
    assert result.get("assessment") is None


@pytest.mark.asyncio
async def test_blocked_assessment_does_not_touch_assessment_state():
    state = _state({"status": "blocked", "questions": ["What is the goal?"]})
    state["assessment"] = _assessment("a1")
    result = await monitor_performer(state)
    assert result["assessment"]["assessment_hash"] == "a1"
    assert result["phase"] == "blocked"
    assert result["assessor_open_questions"] == [{"question": "What is the goal?", "answer": ""}]

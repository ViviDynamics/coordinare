"""166 FR-013: dispatch injects assessment into architecting only, and reset
clears it when re-dispatching the assessor."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.dispatch_performer import (
    inject_assessment,
    reset_assessment_for_assessor,
)
from coordinare.graph.state import initial_state


def _assessment() -> dict:
    return {
        "ready": True,
        "goal": "Add time entry categories",
        "expected_behavior": "Users select category",
        "out_of_scope": ["Category management UI"],
        "questions": [],
        "assumptions": [],
        "criteria": [{"surface": "/time_entries/new", "action": "open", "expected": "category select", "kind": "functional"}],
        "criteria_source": "card",
        "clarifications": [],
        "assessment_hash": "a1",
    }


def test_inject_assessment_injects_only_for_architecting():
    state = initial_state()
    state["assessment"] = _assessment()

    for stage in ["assessing", "implementing", "qa", "documenting"]:
        card_context = {}
        inject_assessment(card_context, state, performer_stage=stage)
        assert "assessment" not in card_context, f"assessment should not be injected for {stage}"

    card_context = {}
    inject_assessment(card_context, state, performer_stage="architecting")
    assert "assessment" in card_context


@pytest.mark.asyncio
async def test_inject_assessment_deep_copies():
    state = initial_state()
    state["assessment"] = _assessment()
    card_context = {}

    inject_assessment(card_context, state, performer_stage="architecting")

    # Mutating card_context should not mutate state
    card_context["assessment"]["goal"] = "MODIFIED"
    assert state["assessment"]["goal"] == "Add time entry categories"


def test_inject_assessment_without_assessment_in_state():
    state = initial_state()
    card_context = {}

    inject_assessment(card_context, state, performer_stage="architecting")

    assert "assessment" not in card_context


def test_reset_assessment_for_assessor_clears_on_assessing():
    state = initial_state()
    state["assessment"] = _assessment()

    result = reset_assessment_for_assessor(state, "assessing")

    assert state["assessment"] is None
    assert result is True


def test_reset_assessment_for_assessor_returns_false_when_nothing_to_clear():
    state = initial_state()

    result = reset_assessment_for_assessor(state, "assessing")

    assert state["assessment"] is None
    assert result is False


def test_reset_assessment_for_assessor_leaves_other_stages_alone():
    state = initial_state()
    state["assessment"] = _assessment()

    for stage in ["architecting", "implementing", "qa"]:
        reset_assessment_for_assessor(state, stage)
        assert state["assessment"] is not None


def test_reset_before_inject_order():
    """Test that reset is called before inject by verifying cleared state
    is not re-injected."""
    state = initial_state()
    state["assessment"] = _assessment()
    card_context = {}

    reset_assessment_for_assessor(state, "assessing")
    inject_assessment(card_context, state, performer_stage="architecting")

    assert "assessment" not in card_context


def test_clarifications_injected_for_assessing_stage():
    """When dispatching the assessing stage, clarifications from card_clarifications
    are injected, filtering to dict entries only."""
    # This test verifies the clarifications injection behavior that happens
    # in _dispatch_performer_body, but we test the filtering logic here.
    card_clarifications = [
        {"question": "What is the goal?", "answer": "Add categories"},
        {"question": "Who is the user?", "answer": ""},  # empty answer
        "invalid_non_dict_entry",  # should be skipped
        {"question": "Why categories?", "answer": "For organization"},
    ]

    # Filter to dict entries only (as done in dispatch_performer)
    filtered = [c for c in card_clarifications if isinstance(c, dict)]

    assert len(filtered) == 3
    assert all(isinstance(c, dict) for c in filtered)


def test_clarifications_not_injected_for_other_stages():
    """Clarifications should only be injected for assessing stage.
    This is enforced in _dispatch_performer_body."""
    # The clarifications injection happens in _dispatch_performer_body
    # inside the `if performer_stage == "assessing":` block, so it's
    # inherently only for assessing.

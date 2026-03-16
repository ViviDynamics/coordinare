from __future__ import annotations

import pytest

from coordinare.graph.nodes.assess_card import assess_card
from coordinare.graph.state import initial_state


class _GitHub:
    async def get_issue_details(self, issue_id: str):
        return {"id": issue_id, "title": "Card"}


class _Backend:
    async def assess(self, card):
        _ = card
        return {"sufficient": False, "questions": ["Need context"]}


class _FailingBackend:
    async def assess(self, card):
        msg = "Backend unavailable"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_assess_card_blocks_on_service_failure() -> None:
    """Spec edge case: assessment failures move card to blocked, not idle."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _FailingBackend()

    result = await assess_card(state)

    assert result["phase"] == "blocked"
    assert any("Assessment failed" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_assess_card_sets_blocked_when_insufficient() -> None:
    state = initial_state()
    state["current_card"] = {"issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _Backend()

    result = await assess_card(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["Need context"]


# ---------------------------------------------------------------------------
# Lines 27-29: clarifications appended to details when present
# ---------------------------------------------------------------------------


class _SufficientBackend:
    async def assess(self, card):
        return {"sufficient": True, "questions": []}


@pytest.mark.asyncio
async def test_assess_card_dispatches_with_no_clarifications() -> None:
    """Lines 62->66: sufficient with no clarifications → skip embedding, set dispatching."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _SufficientBackend()
    # No card_clarifications set

    result = await assess_card(state)

    assert result["phase"] == "dispatching"
    assert result["open_questions"] == []


@pytest.mark.asyncio
async def test_assess_card_merges_clarifications_into_details() -> None:
    """Lines 27-29: when card_clarifications exist, they're merged into the issue details."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _SufficientBackend()
    state["card_clarifications"] = [{"question": "Q?", "answer": "A"}]

    result = await assess_card(state)

    # Sufficient → dispatching
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# Lines 51-57: no new questions after answered rounds → treat as sufficient
# ---------------------------------------------------------------------------


class _InsufficientNoQuestionsBackend:
    """Returns insufficient=False but no questions."""
    async def assess(self, card):
        return {"sufficient": False, "questions": []}


@pytest.mark.asyncio
async def test_assess_card_treats_no_new_questions_after_answers_as_sufficient() -> None:
    """Lines 51-57: insufficient=False + no questions + answered rounds → sufficient."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _InsufficientNoQuestionsBackend()
    # Provide answered clarifications (at least one round answered)
    state["card_clarifications"] = [{"question": "Q?", "answer": "Yes"}]

    result = await assess_card(state)

    # Should be treated as sufficient because there are no new questions
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# Lines 62-67: clarifications embedded into card when sufficient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assess_card_embeds_clarifications_into_card_when_sufficient() -> None:
    """Lines 62-67: clarifications are embedded into current_card when assessment is sufficient."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1", "title": "Fix bug"}
    state["github_service"] = _GitHub()
    state["assessment_backend"] = _SufficientBackend()
    state["card_clarifications"] = [{"question": "Q?", "answer": "A"}]

    result = await assess_card(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"].get("clarifications") == [{"question": "Q?", "answer": "A"}]

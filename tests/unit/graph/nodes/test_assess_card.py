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

from __future__ import annotations

import pytest

from coordinare.graph.nodes.assess_card import assess_card
from coordinare.graph.state import initial_state


class _GitHub:
    async def get_issue_details(self, issue_id: str):
        return {"id": issue_id, "title": "Card"}


class _Claude:
    async def assess_card_sufficiency(self, card):
        _ = card
        return {"sufficient": False, "questions": ["Need context"]}


class _FailingClaude:
    async def assess_card_sufficiency(self, card):
        msg = "Claude API unavailable"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_assess_card_blocks_on_service_failure() -> None:
    """Spec edge case: assessment failures move card to blocked, not idle."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_1", "issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["claude_service"] = _FailingClaude()

    result = await assess_card(state)

    assert result["phase"] == "blocked"
    assert any("Assessment failed" in q for q in result["open_questions"])


@pytest.mark.asyncio
async def test_assess_card_sets_blocked_when_insufficient() -> None:
    state = initial_state()
    state["current_card"] = {"issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["claude_service"] = _Claude()

    result = await assess_card(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["Need context"]

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


@pytest.mark.asyncio
async def test_assess_card_sets_blocked_when_insufficient() -> None:
    state = initial_state()
    state["current_card"] = {"issue_id": "ISSUE_1"}
    state["github_service"] = _GitHub()
    state["claude_service"] = _Claude()

    result = await assess_card(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["Need context"]

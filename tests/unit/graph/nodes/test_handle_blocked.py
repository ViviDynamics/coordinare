from __future__ import annotations

import pytest

from coordinare.graph.nodes.handle_blocked import handle_blocked
from coordinare.graph.state import initial_state


class _GitHub:
    async def move_card(self, item_id: str, status: str) -> None:
        assert item_id == "ITEM_1"
        assert status == "BLOCKED"

    async def add_comment(self, subject_id: str, body: str):
        assert subject_id == "ISSUE_1"
        assert "Needs input" in body
        return {"id": "C1"}


@pytest.mark.asyncio
async def test_handle_blocked_moves_card_and_comments() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()
    state["current_card"] = {"id": "ITEM_1", "issue_id": "ISSUE_1"}
    state["open_questions"] = ["Clarify AC"]

    result = await handle_blocked(state)

    assert result["phase"] == "blocked"
    assert result.get("last_blocked_notified_at") is not None

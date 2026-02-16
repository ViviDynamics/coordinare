from __future__ import annotations

import pytest

from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import initial_state


class _GitHub:
    async def poll_board(self):
        return {
            "snapshot": {"TODO": ["ITEM_1"], "IN_PROGRESS": [], "IN_REVIEW": []},
            "titles": {"ITEM_1": "Card"},
            "descriptions": {"ITEM_1": "Desc"},
            "issue_numbers": {"ITEM_1": 1},
        }


@pytest.mark.asyncio
async def test_check_board_selects_todo_card() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()

    result = await check_board(state)

    assert result["phase"] == "dispatching"
    assert result["current_card"]["id"] == "ITEM_1"

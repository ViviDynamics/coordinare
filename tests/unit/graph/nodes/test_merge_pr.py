from __future__ import annotations

import pytest

from coordinare.graph.nodes.merge_pr import merge_pr
from coordinare.graph.state import initial_state


class _GitHub:
    async def check_mergeability(self, pr_id: str):
        _ = pr_id
        return {"mergeable": True}

    async def squash_merge(self, pr_id: str):
        _ = pr_id
        return {"merged": True}

    async def move_card(self, item_id: str, status: str) -> None:
        assert item_id == "ITEM_1"
        assert status == "DONE"


@pytest.mark.asyncio
async def test_merge_pr_moves_card_to_done() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_1", "status": "IN_REVIEW"}

    result = await merge_pr(state)

    assert result["phase"] == "idle"
    assert result["current_card"]["status"] == "DONE"

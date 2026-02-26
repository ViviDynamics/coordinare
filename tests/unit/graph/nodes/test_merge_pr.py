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


class _GitHubWithMergeCommit:
    async def check_mergeability(self, pr_id: str):
        return {"mergeable": True}

    async def squash_merge(self, pr_id: str):
        return {
            "merged": True,
            "id": "PR_1",
            "merge_commit": {"oid": "abc1234def5678", "messageHeadline": "Fix bug (#42)"},
        }

    async def move_card(self, item_id: str, status: str) -> None:
        pass


@pytest.mark.asyncio
async def test_merge_pr_moves_card_to_done() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_1", "status": "IN_REVIEW"}

    result = await merge_pr(state)

    assert result["phase"] == "idle"
    assert result["current_card"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_merge_pr_populates_commit_summary() -> None:
    state = initial_state()
    state["github_service"] = _GitHubWithMergeCommit()
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_1", "status": "IN_REVIEW"}

    result = await merge_pr(state)

    assert result["commit_summary"] == "abc1234 Fix bug (#42)"
    assert result["current_card"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_merge_pr_commit_summary_none_when_no_merge_commit() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_1", "status": "IN_REVIEW"}

    result = await merge_pr(state)

    assert result.get("commit_summary") is None


@pytest.mark.asyncio
async def test_merge_pr_idle_when_no_github() -> None:
    state = initial_state()
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_1"}

    result = await merge_pr(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_merge_pr_idle_when_no_card() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()

    result = await merge_pr(state)

    assert result["phase"] == "idle"


class _GitHubNotMergeable:
    async def check_mergeability(self, pr_id: str):
        return {"mergeable": False, "reason": "conflicts"}


@pytest.mark.asyncio
async def test_merge_pr_blocks_when_not_mergeable() -> None:
    state = initial_state()
    state["github_service"] = _GitHubNotMergeable()
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_1", "status": "IN_REVIEW"}

    result = await merge_pr(state)

    assert result["phase"] == "blocked"
    assert any("merge conflicts" in q for q in result["open_questions"])

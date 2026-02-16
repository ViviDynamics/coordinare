from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state


class _GitHub:
    async def get_pr_reviews(self, pr_id: str):
        _ = pr_id
        return [
            {"author_login": "copilot[bot]", "state": "COMMENTED", "body": "bot"},
            {"author_login": "alice", "state": "APPROVED", "body": "ship"},
        ]


@pytest.mark.asyncio
async def test_monitor_pr_routes_to_merge_on_human_approval() -> None:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _GitHub()

    result = await monitor_pr(state)

    assert result["phase"] == "merging"

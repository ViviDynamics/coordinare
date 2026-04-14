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


# ---------------------------------------------------------------------------
# 042 — PermanentGitHubError must NOT loop; surface to operator via blocked
# ---------------------------------------------------------------------------


class _GitHubSquashMergePermanent:
    """Simulates a 422 UNPROCESSABLE from GitHub on squash_merge — e.g.,
    the App lacks ruleset bypass permission ("not authorized to push")."""

    async def check_mergeability(self, pr_id: str):
        return {"mergeable": True}

    async def squash_merge(self, pr_id: str):
        from coordinare.services.github import PermanentGitHubError
        raise PermanentGitHubError("You're not authorized to push to this branch.")


@pytest.mark.asyncio
async def test_merge_pr_blocks_on_squash_permanent_error() -> None:
    """042 regression: a permanent error from squash_merge must transition
    to ``blocked`` (not loop back to ``merging``).  Otherwise the coordinare
    retries the same call every cycle until the breaker trips, blocking
    every other GitHub call in the process."""
    state = initial_state()
    state["github_service"] = _GitHubSquashMergePermanent()
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_1", "status": "IN_REVIEW"}

    result = await merge_pr(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"]
    # The actual GitHub error message must surface so the operator can act
    assert any("authorized" in q.lower() for q in result["open_questions"])


class _GitHubMergeabilityPermanent:
    async def check_mergeability(self, pr_id: str):
        from coordinare.services.github import PermanentGitHubError
        raise PermanentGitHubError("Could not resolve to a node with the global id of 'PR_bad'")


@pytest.mark.asyncio
async def test_merge_pr_blocks_on_check_mergeability_permanent_error() -> None:
    """042: A permanent error from check_mergeability (e.g. NOT_FOUND from
    a stale pr_node_id) must also transition to blocked, not loop."""
    state = initial_state()
    state["github_service"] = _GitHubMergeabilityPermanent()
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_bad", "status": "IN_REVIEW"}

    result = await merge_pr(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"]


class _GitHubSquashMergeTransient:
    async def check_mergeability(self, pr_id: str):
        return {"mergeable": True}

    async def squash_merge(self, pr_id: str):
        raise RuntimeError("network blip")


@pytest.mark.asyncio
async def test_merge_pr_loops_on_transient_squash_error() -> None:
    """042: Transient errors (network blips, 5xx) STILL retry — only
    PermanentGitHubError exits the loop.  This protects us from short
    GitHub outages while preventing infinite loops on auth issues."""
    state = initial_state()
    state["github_service"] = _GitHubSquashMergeTransient()
    state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_1", "status": "IN_REVIEW"}

    result = await merge_pr(state)

    # Stays in merging to retry on the next cycle
    assert result["phase"] == "merging"

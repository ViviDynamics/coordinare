"""Spec 073 Phase 9 / US6: skip assess_card when card already has an open PR.

Regression: after the US5 token-refresh fix, a transient WorkspaceSetupError
should no longer requeue cards mid-flight, but a card that already has an
open PR (e.g. cycle re-entry after a transient failure) must never be put
back through ``assess_card`` — its open PR supersedes any stale "needs more
info" comments on the underlying issue.
"""

from __future__ import annotations

import pytest

from coordinare.graph.nodes.assess_card import assess_card
from coordinare.graph.state import initial_state


class _GitHubWithOpenPR:
    """Stub that reports an OPEN PR exists for any issue."""

    async def get_issue_details(self, issue_id: str):
        return {"id": issue_id, "title": "Card"}

    async def find_pr_for_issue(self, issue_node_id: str):
        return {
            "pr_node_id": "PR_NODE_OPEN",
            "pr_url": "https://github.com/x/y/pull/123",
        }


class _GitHubNoPR:
    """Stub that reports no open PR."""

    async def get_issue_details(self, issue_id: str):
        return {"id": issue_id, "title": "Card"}

    async def find_pr_for_issue(self, issue_node_id: str):
        return None


class _BlockingBackend:
    """Returns insufficient — so without the open-PR guard, the card blocks."""

    async def assess(self, card):
        return {
            "sufficient": False,
            "questions": ["Stale clarification question from before PR opened"],
        }


@pytest.mark.asyncio
async def test_assess_short_circuits_when_card_has_open_pr() -> None:
    """Case 1: card with ``pr_url`` set → skip assess, route to monitor_pr."""
    state = initial_state()
    state["current_card"] = {
        "id": "CARD_1",
        "issue_id": "ISSUE_1",
        "pr_url": "https://github.com/x/y/pull/123",
        "pr_node_id": "PR_NODE_OPEN",
    }
    state["github_service"] = _GitHubWithOpenPR()
    state["conducting_backend"] = _BlockingBackend()

    result = await assess_card(state)

    assert result["phase"] == "monitoring_pr"
    # Stale assessment questions must not be propagated.
    assert not result.get("open_questions")


@pytest.mark.asyncio
async def test_assess_runs_when_no_pr_url_and_no_open_pr() -> None:
    """Case 2: no pr_url and find_pr_for_issue returns None → assess runs."""
    state = initial_state()
    state["current_card"] = {"id": "CARD_2", "issue_id": "ISSUE_2"}
    state["github_service"] = _GitHubNoPR()
    state["conducting_backend"] = _BlockingBackend()

    result = await assess_card(state)

    # Backend marked insufficient → blocked + questions surface as before.
    assert result["phase"] == "blocked"
    assert result["open_questions"] == [
        "Stale clarification question from before PR opened",
    ]


class _GitHubClosedPR:
    """pr_url is set on the card but the PR was closed (find_pr_for_issue → None)."""

    async def get_issue_details(self, issue_id: str):
        return {"id": issue_id, "title": "Card"}

    async def find_pr_for_issue(self, issue_node_id: str):
        return None  # no OPEN PR — the recorded pr_url points to a closed one


@pytest.mark.asyncio
async def test_assess_runs_when_pr_url_points_at_closed_pr() -> None:
    """Case 3: card has pr_url but the PR is closed → assess runs normally.

    Don't short-circuit on a stale ``pr_url``; the closed-PR retry-limit logic
    in dispatch_performer must remain the authority for closed-PR handling.
    """
    state = initial_state()
    state["current_card"] = {
        "id": "CARD_3",
        "issue_id": "ISSUE_3",
        "pr_url": "https://github.com/x/y/pull/999",  # closed
        "pr_node_id": "PR_NODE_CLOSED",
    }
    state["github_service"] = _GitHubClosedPR()
    state["conducting_backend"] = _BlockingBackend()

    result = await assess_card(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == [
        "Stale clarification question from before PR opened",
    ]

"""Spec 076 T115 (FR-024 regression) — multi-PR divergence blocks dispatch.

Reconstructs today's #133/#148 anomaly at the dispatch_performer
boundary: when coordinare would launch a fresh implementer for card
#101, the multi-PR check sees TWO open PRs on the canonical prefix
and refuses the dispatch.
"""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state


class _GithubWithTwoOpenPRs:
    """Returns the today's-incident state: PR #133 and #148 both open
    under coordinare/PVTI_TODAY/."""

    async def list_prs_by_branch_prefix(self, owner, repo, prefix, *, state="OPEN", limit=20):
        if prefix == "coordinare/PVTI_TODAY/":
            return [
                {"number": 133, "url": "https://github.com/x/y/pull/133", "head_ref": "coordinare/PVTI_TODAY/schema-old"},
                {"number": 148, "url": "https://github.com/x/y/pull/148", "head_ref": "coordinare/PVTI_TODAY/feat-new"},
            ]
        return []


class _Svc:
    """Service stub that records whether dispatch was actually called."""

    def __init__(self) -> None:
        self.dispatched: bool = False
        self._active_jobs: dict = {}

    def has_live_session(self, session_id: str) -> bool:
        return False

    async def dispatch_card(self, *args, **kwargs):
        self.dispatched = True
        return {"status": "ok", "session_id": "fresh-uuid", "job_id": "new-job", "accepted": True}


@pytest.mark.asyncio
async def test_fr024_dispatch_refused_on_multi_pr_divergence() -> None:
    """The dispatch path MUST short-circuit when > 1 open PR exists for
    the card under the canonical branch prefix.  No new container, no
    side-effects."""
    svc = _Svc()
    state = initial_state()
    state["current_card"] = {
        "id": "PVTI_TODAY",
        "title": "Time tracking",
        "status": "IN_PROGRESS",
        "pr_url": "https://github.com/x/y/pull/133",
    }
    state["performer_stage"] = "implementing"
    state["agent_dispatch"] = {}
    state["performer_services"] = {"implementing": svc}
    state["github_service"] = _GithubWithTwoOpenPRs()

    result = await dispatch_performer(state)

    # Dispatch refused — service.dispatch_card NOT called
    assert svc.dispatched is False
    # State returned with no agent_dispatch session
    assert result is state
    assert result.get("agent_dispatch") == {}

"""509 repro: classify_human_feedback → persist → restore → dispatch_performer.

Incident: fresh implementer pod ran after a CHANGES_REQUESTED review with inline
comments, but the agent showed no trace of the feedback (no push, zero events).
This test drives the real relay chain across a simulated daemon cycle boundary
using the daemon's own persist/restore helpers and asserts the dispatch payload
carries the review comments verbatim.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import _persist_one_session, _restored_session_dict
from coordinare.graph.nodes.classify_human_feedback import classify_human_feedback
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state
from coordinare.session import session_to_state, state_to_session
from coordinare.state_store import WorkflowSnapshot


def _incident_state() -> dict:
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "title": "Fix clamp bug",
        "description": "desc",
        "status": "IN_PROGRESS",
        "pr_url": "https://github.com/o/r/pull/5",
        "pr_node_id": "PR_5",
    }
    state["active_sessions"] = {}
    state["lifecycle_sequence"] = ["implementing", "reviewing", "closing_review"]
    state["performer_stage"] = "implementing"
    state["phase"] = "monitoring_pr"
    state["performer_services"] = {"implementing": _SpyService()}
    state["github_service"] = AsyncMock()
    return state


class _SpyService:
    def __init__(self) -> None:
        self.dispatched: list[dict] = []

    async def check_health(self) -> dict:
        return {"status": "ok"}

    async def dispatch_card(self, card_context: dict, **kwargs) -> dict:
        self.dispatched.append(card_context)
        return {"session_id": "s-1", "status": "dispatched"}


_REVIEW = {
    "id": "PRR_abc",
    "author_login": "alice",
    "state": "CHANGES_REQUESTED",
    "body": "Two things to fix.",
    "submitted_at": "2026-10-02T12:00:00Z",
    "commit_oid": "cf40c78",
    "author_type": "User",
    "comments": [
        {"body": "Clamp is off by one", "path": "src/x.py", "line": 42},
        {"body": "Missing edge-case tests", "path": "tests/test_x.py", "line": None},
    ],
}


def _merge(state: dict, result: dict) -> dict:
    state.update(result)
    return state


@pytest.mark.asyncio
async def test_feedback_survives_cycle_boundary_to_dispatch() -> None:
    state = _incident_state()
    state["pending_reviews"] = [_REVIEW]

    result = await classify_human_feedback(state)
    state.update(result)
    assert state["relay_feedback"] == [_REVIEW]
    assert state["phase"] == "dispatching"

    sess = state_to_session(state)
    persisted = _persist_one_session("card-1", dict(sess))
    snap = WorkflowSnapshot(
        snapshot_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        phase="dispatching",
    )
    restored = _restored_session_dict("card-1", persisted, snap, state["current_card"])
    fresh = _incident_state()
    fresh["active_sessions"] = {"card-1": dict(restored)}
    session_to_state(restored, fresh)

    assert fresh["relay_feedback"] == [_REVIEW], "relay_feedback lost across cycle boundary"

    svc = _SpyService()
    fresh["performer_services"] = {"implementing": svc}
    await dispatch_performer(fresh)

    assert len(svc.dispatched) == 1
    ctx = svc.dispatched[0]
    assert ctx.get("relay_feedback") == [_REVIEW], (
        "dispatch card_context lost relay_feedback"
    )
    inline = ctx["relay_feedback"][0]["comments"]
    assert inline == _REVIEW["comments"]

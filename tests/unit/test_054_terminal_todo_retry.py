"""A Todo retry resumes a blocked turn only after all owned writers stop."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon, _persist_one_session, _restored_session_dict
from coordinare.graph.nodes.check_board import _reset_and_rehydrate
from coordinare.state_store import PersistedSession, WorkflowSnapshot


def blocked_owner(*, stopped=True, side=False):
    service = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=stopped))
    owner = {
        "current_card": {"id": "story", "status": "BLOCKED", "pr_url": "https://github.com/example/sample/pull/1"},
        "phase": "blocked", "performer_stage": "assessing", "board_paused": False,
        "agent_dispatch": {"session_id": "terminal", "performer_id": "worker"},
        "relay_feedback": [{"message": "human instruction"}],
        "open_questions": ["A previous blocker"], "feedback_cycle_count": 3,
    }
    sibling = {"current_card": {"id": "peer", "status": "IN_PROGRESS"}, "phase": "monitoring_performer",
               "agent_dispatch": {"session_id": "peer-worker", "performer_id": "peer"}}
    d = CoordinareDaemon(None)
    d.state.update(active_sessions={"story": owner, "peer": sibling}, current_card=owner["current_card"],
                   active_card_id="story", phase="blocked", board_snapshot={"TODO": ["story"], "IN_PROGRESS": ["peer"]},
                   performer_services_by_id={"worker": service}, performer_services={"documenting": service})
    if side:
        owner["documenting_side"] = {"blueprint_hash": "synthetic-blueprint", "session_id": "side", "status": "running", "writer_active": True}
    return d, owner, service, deepcopy(sibling)


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("side", [False, True])
@pytest.mark.parametrize("restore", [False, True])
async def test_blocked_todo_retries_after_confirmed_stop_preserving_history(uncertain, side, restore):
    d, owner, service, peer = blocked_owner(stopped=not uncertain, side=side)
    if uncertain:
        await d._reconcile_board_pauses()
        await _reset_and_rehydrate(d.state, d.state["board_snapshot"], ["story"], 2, d.state["active_sessions"])
        assert owner["phase"] == "monitoring_performer"
        assert owner["board_paused"] is True
        assert owner["agent_dispatch"]["session_id"] == "terminal"
        if side:
            assert owner["documenting_side"]["writer_active"] is True
        service.stop_session_confirmed.return_value = True
    if restore:
        saved = PersistedSession.model_validate_json(_persist_one_session("story", owner).model_dump_json())
        owner = _restored_session_dict("story", saved, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked"), owner["current_card"])
        d.state["active_sessions"]["story"] = owner
    await d._reconcile_board_pauses()
    assert owner["agent_dispatch"] == {}
    assert owner["board_paused"] is False
    await _reset_and_rehydrate(d.state, d.state["board_snapshot"], ["story"], 2, d.state["active_sessions"])
    assert owner["phase"] == "dispatching"
    assert owner["feedback_cycle_count"] == 0
    assert owner["open_questions"] == []
    assert owner["relay_feedback"] == [{"message": "human instruction"}]
    assert owner["current_card"]["pr_url"] == "https://github.com/example/sample/pull/1"
    assert d.state["active_sessions"]["peer"] == peer
    count = service.stop_session_confirmed.await_count
    await d._reconcile_board_pauses()
    assert service.stop_session_confirmed.await_count == count
    assert owner["phase"] == "dispatching"
    if side:
        assert owner["documenting_side"]["writer_active"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("phase", "column", "handoff"), [("monitoring_performer", "TODO", False),
    ("blocked", "BACKLOG", False), ("blocked", "TODO", True)])
async def test_working_backlog_and_completed_handoff_remain_paused(phase, column, handoff):
    d, owner, service, peer = blocked_owner()
    owner["phase"] = phase
    d.state["board_snapshot"] = {column: ["story"], "IN_PROGRESS": ["peer"]}
    if handoff:
        owner["pending_pr_handoff"] = {"status": "complete", "pr_url": owner["current_card"]["pr_url"]}
    await d._reconcile_board_pauses()
    assert owner["board_paused"] is True
    assert owner["phase"] == "blocked"
    assert owner["agent_dispatch"] == {}
    assert d.state["active_sessions"]["peer"] == peer
    service.stop_session_confirmed.assert_awaited_once_with("terminal")

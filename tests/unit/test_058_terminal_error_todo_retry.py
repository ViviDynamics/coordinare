"""Explicit Todo recovery must not strand a confirmed terminal-error owner."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.daemon import _persist_one_session, _restored_session_dict
from coordinare.graph.nodes.check_board import _reset_and_rehydrate
from coordinare.state_store import PersistedSession, WorkflowSnapshot
from tests.unit.test_054_terminal_todo_retry import blocked_owner


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("side", [False, True])
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("card_status", ["BLOCKED", "IN_PROGRESS"])
async def test_terminal_error_todo_retries_only_after_owned_stop(uncertain, side, restore, card_status):
    d, owner, service, peer = blocked_owner(stopped=not uncertain, side=side)
    owner.update(phase="system_error", system_error_count=2,
                 system_error_reason="BACKEND_FORMAT_ERROR: malformed assessment")
    owner["current_card"]["status"] = card_status
    owner["card_clarifications"] = [{"questions": ["Empty input?"], "answer": "Return empty string"}]
    owner["head_at_last_turn"] = "retained-head"
    owner["idle_timeout_retries"] = {"story:assessing": {
        "card_id": "story", "performer_stage": "assessing", "attempt_count": 2,
        "window_start_at": "2026-10-10T10:00:00+00:00", "last_at": "2026-10-10T10:01:00+00:00"}}
    if uncertain:
        await d._reconcile_board_pauses()
        assert owner["phase"] == "monitoring_performer"
        assert owner["board_paused"] is True
        assert owner["agent_dispatch"]["session_id"] == "terminal"
        if side:
            assert owner["documenting_side"]["writer_active"] is True
        service.stop_session_confirmed.return_value = True
    if restore:
        saved = PersistedSession.model_validate_json(_persist_one_session("story", owner).model_dump_json())
        owner = _restored_session_dict("story", saved,
            WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="system_error"), owner["current_card"])
        d.state["active_sessions"]["story"] = owner
    await d._reconcile_board_pauses()
    assert owner["agent_dispatch"] == {}
    assert owner["board_paused"] is False
    await _reset_and_rehydrate(d.state, d.state["board_snapshot"], ["story"], 2, d.state["active_sessions"])
    assert owner["phase"] == "dispatching"
    assert owner["performer_stage"] == "assessing"
    assert owner["system_error_count"] == 2
    assert owner["idle_timeout_retries"]["story:assessing"]["attempt_count"] == 2
    assert owner["card_clarifications"] == [{"questions": ["Empty input?"], "answer": "Return empty string"}]
    assert owner["relay_feedback"] == [{"message": "human instruction"}]
    assert owner["head_at_last_turn"] == "retained-head"
    assert owner["current_card"]["pr_url"] == "https://github.com/example/sample/pull/1"
    assert d.state["active_sessions"]["peer"] == peer
    count = service.stop_session_confirmed.await_count
    await d._reconcile_board_pauses()
    assert service.stop_session_confirmed.await_count == count
    assert owner["phase"] == "dispatching"
    if side:
        assert owner["documenting_side"]["writer_active"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("column", "handoff"), [("BACKLOG", False), ("TODO", True)])
async def test_terminal_error_backlog_and_completed_handoff_stay_paused(column, handoff):
    d, owner, service, peer = blocked_owner()
    owner["phase"] = "system_error"
    d.state["board_snapshot"] = {column: ["story"], "IN_PROGRESS": ["peer"]}
    if handoff:
        owner["pending_pr_handoff"] = {"status": "complete", "pr_url": owner["current_card"]["pr_url"]}
    await d._reconcile_board_pauses()
    assert owner["board_paused"] is True
    assert owner["phase"] == "blocked"
    assert owner["agent_dispatch"] == {}
    assert d.state["active_sessions"]["peer"] == peer
    service.stop_session_confirmed.assert_awaited_once_with("terminal")


@pytest.mark.asyncio
async def test_stale_board_never_admits_terminal_error_retry():
    d, owner, service, peer = blocked_owner()
    owner["phase"] = "system_error"
    await d._reconcile_board_pauses(board_is_fresh=False)
    assert owner["phase"] == "system_error"
    assert owner["agent_dispatch"]["session_id"] == "terminal"
    assert d.state["active_sessions"]["peer"] == peer
    service.stop_session_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_pr_error_retry_preserves_closed_pr_gate():
    d, owner, _, _ = blocked_owner()
    owner.update(phase="system_error", system_error_reason="PR closed without merging: https://github.com/example/sample/pull/1. Closed")
    await d._reconcile_board_pauses()
    await _reset_and_rehydrate(d.state, d.state["board_snapshot"], ["story"], 2, d.state["active_sessions"])
    assert owner["board_paused"] is False
    assert owner["phase"] == "blocked"
    assert owner["system_error_reason"].startswith("PR closed without merging:")
    assert owner["current_card"]["pr_url"] == "https://github.com/example/sample/pull/1"

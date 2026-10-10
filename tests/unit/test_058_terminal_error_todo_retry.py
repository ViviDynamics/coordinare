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
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("through_backlog", [False, True])
@pytest.mark.parametrize("count", [2, 3])
async def test_cold_terminal_todo_retry_keeps_budget_after_dispatch(legacy, through_backlog, count):
    from unittest.mock import patch

    from coordinare.config import PersonasConfig
    from coordinare.graph.nodes.dispatch_performer import dispatch_performer
    from coordinare.session import session_to_state
    from tests.unit.graph.nodes.test_dispatch_performer import _base_state, _Service

    d, owner, _, _ = blocked_owner()
    last_at = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
    owner.update(phase="system_error", system_error_count=count,
                 system_error_last_at=last_at, system_error_notified=False)
    if through_backlog:
        d.state["board_snapshot"] = {"BACKLOG": ["story"]}
        await d._reconcile_board_pauses()
    payload = _persist_one_session("story", owner).model_dump(mode="json")
    if legacy:
        payload.pop("system_error_last_at", None)
    owner = _restored_session_dict("story", PersistedSession.model_validate(payload),
        WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked"), owner["current_card"])
    d.state["active_sessions"]["story"] = owner
    d.state["board_snapshot"] = {"TODO": ["story"]}
    await d._reconcile_board_pauses()
    await _reset_and_rehydrate(d.state, d.state["board_snapshot"], ["story"], 2, d.state["active_sessions"])
    # A second restart after reconciliation must retain the retry handoff.
    owner = _restored_session_dict("story",
        PersistedSession.model_validate_json(_persist_one_session("story", owner).model_dump_json()),
        WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="dispatching"), owner["current_card"])
    service = _Service()
    flat = _base_state(performer_services={"assessing": service}, lifecycle_sequence=["assessing"])
    from unittest.mock import AsyncMock

    flat["github_service"].list_prs_by_branch_prefix = AsyncMock(return_value=[])
    session_to_state(owner, flat)
    with patch("coordinare.graph.nodes.dispatch_performer.load_personas_hot", return_value=PersonasConfig()):
        result = await dispatch_performer(flat)
    assert len(service.dispatched) == 1
    assert result["phase"] == "monitoring_performer"
    assert result["system_error_count"] == count
    assert result["system_error_last_at"] is not None
    if not legacy:
        assert result["system_error_last_at"] == last_at
    assert result["current_card"]["pr_url"] == "https://github.com/example/sample/pull/1"


@pytest.mark.parametrize("version", range(1, 34))
def test_legacy_snapshot_defaults_missing_system_error_clock(version):
    snapshot = WorkflowSnapshot.model_validate({
        "schema_version": version, "snapshot_at": datetime.now(UTC), "phase": "blocked",
        "active_sessions": {"story": {"card_id": "story", "system_error_count": 3}},
    })
    session = snapshot.active_sessions["story"]
    assert session.model_dump()["system_error_last_at"] is None
    assert session.system_error_count == 3


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


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("card_status", ["BLOCKED", "IN_PROGRESS"])
async def test_terminal_error_backlog_then_todo_uses_explicit_retry(restore, card_status):
    d, owner, service, peer = blocked_owner()
    owner.update(phase="system_error", system_error_count=3,
                 system_error_reason="BACKEND_FORMAT_ERROR: malformed assessment")
    owner["current_card"]["status"] = card_status
    owner["card_clarifications"] = [{"questions": ["Empty input?"], "answer": "Return empty string"}]
    d.state["board_snapshot"] = {"BACKLOG": ["story"], "IN_PROGRESS": ["peer"]}
    await d._reconcile_board_pauses()
    assert owner["board_paused"] is True
    assert owner["board_pause_resume_phase"] == "system_error"
    assert owner["agent_dispatch"] == {}
    service.stop_session_confirmed.assert_awaited_once_with("terminal")
    if restore:
        saved = PersistedSession.model_validate_json(_persist_one_session("story", owner).model_dump_json())
        owner = _restored_session_dict("story", saved,
            WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked"), owner["current_card"])
        d.state["active_sessions"]["story"] = owner
    d.state["board_snapshot"] = {"TODO": ["story"], "IN_PROGRESS": ["peer"]}
    await d._reconcile_board_pauses()
    assert owner["board_paused"] is False
    assert owner["phase"] == "blocked"
    await _reset_and_rehydrate(d.state, d.state["board_snapshot"], ["story"], 2, d.state["active_sessions"])
    assert owner["phase"] == "dispatching"
    assert owner["system_error_count"] == 3
    assert owner["card_clarifications"] == [{"questions": ["Empty input?"], "answer": "Return empty string"}]
    assert owner["relay_feedback"] == [{"message": "human instruction"}]
    assert owner["current_card"]["pr_url"] == "https://github.com/example/sample/pull/1"
    assert d.state["active_sessions"]["peer"] == peer
    service.stop_session_confirmed.assert_awaited_once_with("terminal")

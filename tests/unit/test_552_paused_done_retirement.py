from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.services.reconciliation import reconcile_board_state


def paused_state():
    card = {"id": "paused", "status": "BLOCKED"}
    session = {
        "current_card": card, "phase": "monitoring_performer",
        "performer_stage": "implementing", "board_paused": True,
        "board_pause_column": "BACKLOG",
        "agent_dispatch": {"session_id": "owned", "performer_id": "worker"},
    }
    sibling = {"current_card": {"id": "sibling"}, "phase": "dispatching"}
    return {
        "active_card": card, "current_card": card, "active_card_id": "paused",
        "phase": "monitoring_performer", "agent_dispatch": session["agent_dispatch"],
        "active_sessions": {"paused": session, "sibling": sibling},
    }


def test_quiescent_paused_done_retires_session_without_touching_sibling():
    state = paused_state()
    original = state["active_sessions"]["paused"]
    original["agent_dispatch"] = {}
    result = reconcile_board_state(state, {"DONE": ["paused"], "TODO": ["sibling"]})
    assert result["action"] == "done_session_retired"
    assert result["retired_session"] is original
    assert result["retired_session"]["agent_dispatch"] == {}
    assert set(state["active_sessions"]) == {"sibling"}
    assert state["phase"] == "idle"
    assert state["active_card_id"] is None
    assert state["current_card"] is None
    assert state["agent_dispatch"] == {}


@pytest.mark.parametrize("column", ["BACKLOG", "TODO", "IN_PROGRESS", "IN_REVIEW", "BLOCKED"])
def test_nonterminal_pause_still_defers_reconciliation(column):
    state = paused_state()
    original = state["active_sessions"]["paused"]
    assert reconcile_board_state(state, {column: ["paused"]})["action"] == "deferred"
    assert state["active_sessions"]["paused"] is original
    assert state["active_card_id"] == "paused"
    assert original["agent_dispatch"]["session_id"] == "owned"


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_normal_done_cleanup_uses_owned_worker_and_never_revives_terminal_session(cleanup_fails):
    service = SimpleNamespace(
        stop_session_confirmed=AsyncMock(return_value=False),
        release_session=AsyncMock(side_effect=RuntimeError("runtime unavailable") if cleanup_fails else None),
    )
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    daemon.state.update(paused_state())
    daemon.state["active_sessions"]["paused"]["board_paused"] = False
    daemon.state.update(board_snapshot={"DONE": ["paused"], "TODO": ["sibling"]},
                        performer_services_by_id={"worker": service})
    await daemon._reconcile_board_pauses()
    assert daemon.state["active_sessions"]["paused"]["agent_dispatch"]["session_id"] == "owned"
    await daemon._reconcile_board_state_with_release()
    service.stop_session_confirmed.assert_not_awaited()
    service.release_session.assert_awaited_once_with("owned")
    assert set(daemon.state["active_sessions"]) == {"sibling"}
    assert daemon.state["active_card_id"] is None
    assert daemon.state["current_card"] is None
    assert daemon.state["phase"] == "idle"
    await daemon._reconcile_board_state_with_release()
    service.release_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("side_only", [False, True])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_actual_multisession_retires_paused_done_only_after_confirmed_owned_stops(focused, side_only, confirmed):
    async def tick(state):
        return state

    def service():
        return SimpleNamespace(
            stop_session_confirmed=AsyncMock(return_value=confirmed),
            release_session=AsyncMock(),
        )

    main, side, wrong_default = service(), service(), service()
    graph = AsyncMock()
    graph.ainvoke.side_effect = tick
    daemon = CoordinareDaemon(graph, poll_interval_seconds=1, max_cycles=1)
    state = paused_state()
    paused = state["active_sessions"]["paused"]
    paused["documenting_side"] = {
        "status": "failed", "writer_active": True,
        "session_id": "side-owned", "performer_id": "doc-worker",
    }
    if side_only:
        paused["agent_dispatch"] = {}
    sibling = state["active_sessions"]["sibling"]
    sibling.update(performer_stage="reviewing", current_card={"id": "sibling", "status": "IN_PROGRESS"},
                   agent_dispatch={"session_id": "sibling-owned"},
                   dispatched_feedback={"stage": "reviewing", "items": [{"body": "Keep sibling request"}]})
    if not focused:
        state.update(active_card_id="sibling", current_card=sibling["current_card"],
                     active_card=sibling["current_card"], agent_dispatch=sibling["agent_dispatch"],
                     dispatched_feedback=sibling["dispatched_feedback"])
    github = SimpleNamespace(poll_board=AsyncMock(return_value={
        "snapshot": {"DONE": ["paused"], "IN_PROGRESS": ["sibling"]},
    }))
    daemon.state.update(state)
    daemon.state.update(config=SimpleNamespace(max_concurrent_cards=2), github_service=github,
                        performer_services_by_id={"worker": main, "doc-worker": side},
                        performer_services={"documenting": wrong_default})
    await daemon._invoke_multi_session()
    github.poll_board.assert_awaited_once()
    assert graph.ainvoke.await_count == 1
    sibling_after_tick = daemon.state["active_sessions"]["sibling"]
    await daemon._post_cycle_invariants()
    assert set(daemon.state["active_sessions"]) == ({"sibling"} if confirmed else {"paused", "sibling"})
    assert daemon.state["active_sessions"]["sibling"] is sibling_after_tick
    assert sibling_after_tick["dispatched_feedback"] == sibling["dispatched_feedback"]
    side.stop_session_confirmed.assert_awaited_once_with("side-owned")
    side.release_session.assert_not_awaited()
    wrong_default.release_session.assert_not_awaited()
    main.release_session.assert_not_awaited()
    assert main.stop_session_confirmed.await_count == (0 if side_only else 1)
    if not side_only:
        main.stop_session_confirmed.assert_awaited_once_with("owned")
    if not focused:
        assert daemon.state["active_card_id"] == "sibling"
        assert daemon.state["current_card"] is sibling_after_tick["current_card"]
        assert daemon.state["agent_dispatch"]["session_id"] == "sibling-owned"
        assert daemon.state["dispatched_feedback"] == sibling["dispatched_feedback"]
    await daemon._post_cycle_invariants()
    side.stop_session_confirmed.assert_awaited_once()

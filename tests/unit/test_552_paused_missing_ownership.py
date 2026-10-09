from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.graph.state import SymphonyRuntimeState
from coordinare.services.reconciliation import collect_active_session_ids
from coordinare.session import create_session_from_card, session_to_state
from coordinare.state_store import WorkflowSnapshot


def restored_daemon(*, focused, main_owned):
    original = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    target = create_session_from_card({"id": "target", "status": "BLOCKED"})
    target.update(phase="blocked", performer_stage="implementing", board_paused=True,
                  board_pause_column="BACKLOG",
                  agent_dispatch={"session_id": "main", "performer_id": "worker"} if main_owned else {},
                  documenting_side={"blueprint_hash": "bp", "status": "running", "writer_active": True,
                                    "session_id": "side", "performer_id": "doc-worker"})
    sibling = create_session_from_card({"id": "sibling", "status": "IN_PROGRESS"})
    sibling.update(phase="monitoring_performer", performer_stage="reviewing",
                   agent_dispatch={"session_id": "sibling-job"},
                   dispatched_feedback={"stage": "reviewing", "items": [{"body": "Keep this request"}]})
    original.state.update(active_sessions={"target": target, "sibling": sibling},
                          active_card_id="target" if focused else "sibling")
    session_to_state(target if focused else sibling, original.state)
    snapshot = WorkflowSnapshot.model_validate_json(original._build_snapshot().model_dump_json())

    async def tick(state):
        return state

    graph = AsyncMock()
    graph.ainvoke.side_effect = tick
    daemon = CoordinareDaemon(graph, poll_interval_seconds=1, max_cycles=1)
    daemon._state_store = SimpleNamespace(load=AsyncMock(return_value=snapshot))
    main = SimpleNamespace(_config=SimpleNamespace(mode="persistent"),
                           stop_session_confirmed=AsyncMock(return_value=False), release_session=AsyncMock())
    side = SimpleNamespace(_config=SimpleNamespace(mode="persistent"),
                           stop_session_confirmed=AsyncMock(return_value=False), release_session=AsyncMock())
    daemon.state.update(config=SimpleNamespace(max_concurrent_cards=2),
                        performer_services_by_id={"worker": main, "doc-worker": side},
                        performer_services={"documenting": side})
    return daemon, main, side


def board(terminal):
    snapshot = {"IN_PROGRESS": ["sibling"]}
    if terminal == "DONE":
        snapshot["DONE"] = ["target"]
    return {"snapshot": snapshot}


@pytest.mark.asyncio
@pytest.mark.parametrize("focused", [True, False])
@pytest.mark.parametrize("main_owned", [True, False])
@pytest.mark.parametrize("terminal", ["missing", "DONE"])
@pytest.mark.parametrize("per_symphony", [False, True])
async def test_json_restart_and_real_cycles_retain_terminal_paused_owners_until_confirmed(focused, main_owned, terminal, per_symphony):
    daemon, main, side = restored_daemon(focused=focused, main_owned=main_owned)
    github = SimpleNamespace(project_id=1, poll_board=AsyncMock(return_value=board(terminal)))
    daemon.state["github_service"] = github
    if per_symphony:
        daemon.state.update(symphony_states={"alpha": SymphonyRuntimeState(name="alpha")},
                            symphony_github_services={"alpha": github})
        daemon._running = True
        daemon._run_env_cache_and_cardless_cycle = AsyncMock()
    await daemon._startup_load_snapshot()
    assert "target" in daemon.state["active_sessions"]
    assert collect_active_session_ids(daemon.state) >= ({"main", "side"} if main_owned else {"side"})
    await daemon._startup_reconciliation_pass()
    async def cycle():
        if per_symphony:
            await daemon._run_orchestration({"alpha": SimpleNamespace()})
        else:
            await daemon._invoke_multi_session()
        await daemon._post_cycle_invariants()

    await cycle()
    retained = daemon.state["active_sessions"]["target"]
    assert retained["documenting_side"]["session_id"] == "side"
    assert retained["phase"] == "monitoring_performer"
    sibling = daemon.state["active_sessions"]["sibling"]
    main.stop_session_confirmed.return_value = True
    side.stop_session_confirmed.return_value = True
    await cycle()
    assert set(daemon.state["active_sessions"]) == {"sibling"}
    assert daemon.state["active_sessions"]["sibling"]["dispatched_feedback"] == sibling["dispatched_feedback"]
    if not focused:
        assert daemon.state["active_card_id"] == "sibling"
        assert daemon.state["agent_dispatch"]["session_id"] == "sibling-job"
    main.release_session.assert_not_awaited()
    side.release_session.assert_not_awaited()
    assert main.stop_session_confirmed.await_count == (2 if main_owned else 0)
    assert side.stop_session_confirmed.await_count == 2
    if per_symphony:
        assert "target" not in daemon.state["symphony_states"]["alpha"].active_sessions
        await cycle()
        assert "target" not in daemon.state["active_sessions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("focused", [True, False])
@pytest.mark.parametrize("response", [RuntimeError("offline"), {}, {"snapshot": None}, {"snapshot": {"DONE": "target"}}])
async def test_unreadable_startup_board_does_not_infer_missing_and_drop_owners(focused, response):
    daemon, _, _ = restored_daemon(focused=focused, main_owned=True)
    github = SimpleNamespace(project_id=1, poll_board=AsyncMock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=response if not isinstance(response, Exception) else None,
    ))
    daemon.state["github_service"] = github
    await daemon._startup_load_snapshot()
    retained = daemon.state["active_sessions"]["target"]
    assert retained["agent_dispatch"]["session_id"] == "main"
    assert retained["documenting_side"]["session_id"] == "side"


@pytest.mark.asyncio
@pytest.mark.parametrize("focused", [True, False])
@pytest.mark.parametrize("response", [RuntimeError("offline"), {}, {"snapshot": None}, {"snapshot": {"DONE": "target"}}])
async def test_failed_cycle_preserves_quiescent_paused_missing_until_fresh_absence(focused, response):
    daemon, main, side = restored_daemon(focused=focused, main_owned=True)
    daemon.state["github_service"] = SimpleNamespace(project_id=1, poll_board=AsyncMock(return_value=board("DONE")))
    await daemon._startup_load_snapshot()
    main.stop_session_confirmed.return_value = True
    side.stop_session_confirmed.return_value = True
    daemon.state["board_snapshot"] = board("missing")["snapshot"]
    daemon.state["github_service"].poll_board = AsyncMock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=response if not isinstance(response, Exception) else None,
    )
    await daemon._invoke_multi_session()
    await daemon._post_cycle_invariants()
    assert "target" in daemon.state["active_sessions"]
    assert daemon.state["active_sessions"]["target"]["agent_dispatch"] == {}
    daemon.state["github_service"].poll_board = AsyncMock(return_value=board("missing"))
    await daemon._invoke_multi_session()
    await daemon._post_cycle_invariants()
    assert "target" not in daemon.state["active_sessions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("focused", [True, False])
async def test_fresh_empty_board_retires_paused_missing_after_confirmed_stops(focused):
    daemon, main, side = restored_daemon(focused=focused, main_owned=True)
    daemon.state["github_service"] = SimpleNamespace(project_id=1, poll_board=AsyncMock(return_value={"snapshot": {}}))
    await daemon._startup_load_snapshot()
    assert "target" in daemon.state["active_sessions"]
    main.stop_session_confirmed.return_value = True
    side.stop_session_confirmed.return_value = True
    await daemon._invoke_multi_session()
    await daemon._post_cycle_invariants()
    assert "target" not in daemon.state["active_sessions"]

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.graph.state import SymphonyRuntimeState
from coordinare.session import create_session_from_card, session_to_state


def side_daemon():
    async def tick(state):
        return state

    graph = AsyncMock()
    graph.ainvoke.side_effect = tick
    daemon = CoordinareDaemon(graph, poll_interval_seconds=1, max_cycles=1)
    daemon._running = True
    daemon.state["config"] = SimpleNamespace(max_concurrent_cards=2)
    daemon._run_env_cache_and_cardless_cycle = AsyncMock()
    service = SimpleNamespace(dispatch_card=AsyncMock(return_value={"session_id": "doc-job"}),
                              check_status=AsyncMock(return_value={"status": "docs_committed"}))
    daemon.state["performer_services"] = {"documenting": service}

    async def resolve(card_id, session, services):
        return {"id": card_id}, None

    daemon._resolve_documenting_side = resolve
    return daemon, service


def candidate(card_id):
    session = create_session_from_card({"id": card_id, "status": "IN_PROGRESS"})
    session.update(phase="monitoring_pr", performer_stage="implementing",
                   blueprint={"blueprint_hash": f"bp-{card_id}",
                              "docs": [{"topic": "Guide", "location": "docs/guide.md", "say": "Describe changes"}]})
    return session


async def finish_polls(daemon):
    if daemon._documenting_side_tasks:
        await asyncio.wait_for(asyncio.gather(*daemon._documenting_side_tasks), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["TODO", "BACKLOG", "BLOCKED", "DONE", "missing", "IN_PROGRESS", "IN_REVIEW"])
@pytest.mark.parametrize("scoped", [False, True])
async def test_actual_begin_and_cycle_only_dispatch_side_writer_on_fresh_active_board(column, scoped):
    daemon, service = side_daemon()
    session = candidate("card")
    daemon.state.update(active_sessions={"card": session}, active_card_id="card", board_snapshot={"IN_PROGRESS": ["card"]})
    session_to_state(session, daemon.state)
    snapshot = {} if column == "missing" else {column: ["card"]}
    github = SimpleNamespace(poll_board=AsyncMock(return_value={"snapshot": snapshot}))
    daemon.state["github_service"] = github
    configs = {}
    if scoped:
        runtime = SymphonyRuntimeState(name="alpha", active_sessions={"card": session}, active_card=session["current_card"])
        daemon.state.update(symphony_states={"alpha": runtime}, symphony_github_services={"alpha": github})
        configs = {"alpha": SimpleNamespace()}
    try:
        await daemon._begin_cycle()
        service.dispatch_card.assert_not_awaited()
        await daemon._run_orchestration(configs)
        assert service.dispatch_card.await_count == int(column in {"IN_PROGRESS", "IN_REVIEW"})
        github.poll_board.assert_awaited_once()
    finally:
        await finish_polls(daemon)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [RuntimeError("offline"), {}, {"snapshot": None}, {"snapshot": {"IN_PROGRESS": "card"}}])
async def test_failed_or_malformed_poll_cannot_authorize_side_writer_from_stale_board(response):
    daemon, service = side_daemon()
    session = candidate("card")
    daemon.state.update(active_sessions={"card": session}, active_card_id="card", board_snapshot={"IN_PROGRESS": ["card"]})
    session_to_state(session, daemon.state)
    daemon.state["github_service"] = SimpleNamespace(poll_board=AsyncMock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=response if not isinstance(response, Exception) else None,
    ))
    try:
        await daemon._begin_cycle()
        await daemon._run_orchestration({})
        service.dispatch_card.assert_not_awaited()
    finally:
        await finish_polls(daemon)


@pytest.mark.asyncio
async def test_multisymphony_side_dispatch_uses_each_own_board_once():
    daemon, service = side_daemon()
    sessions = {cid: candidate(cid) for cid in ("paused", "active")}
    runtimes = {name: SymphonyRuntimeState(name=name, active_sessions={cid: sessions[cid]}, active_card=sessions[cid]["current_card"])
                for name, cid in (("alpha", "paused"), ("beta", "active"))}
    githubs = {"alpha": SimpleNamespace(poll_board=AsyncMock(return_value={"snapshot": {"TODO": ["paused"]}})),
               "beta": SimpleNamespace(poll_board=AsyncMock(return_value={"snapshot": {"IN_REVIEW": ["active"]}}))}
    daemon.state.update(active_sessions=sessions, symphony_states=runtimes, symphony_github_services=githubs,
                        board_snapshot={"IN_PROGRESS": ["paused", "active"]})
    try:
        await daemon._begin_cycle()
        service.dispatch_card.assert_not_awaited()
        await daemon._run_orchestration({"alpha": SimpleNamespace(), "beta": SimpleNamespace()})
        assert [call.args[0]["id"] for call in service.dispatch_card.await_args_list] == ["active"]
        for github in githubs.values():
            github.poll_board.assert_awaited_once()
    finally:
        await finish_polls(daemon)


@pytest.mark.asyncio
async def test_env_bootstrap_early_failure_never_starts_unpolled_side_writer():
    daemon, service = side_daemon()
    daemon.state["active_sessions"] = {"card": candidate("card")}
    daemon._run_env_cache_and_cardless_cycle.side_effect = RuntimeError("bootstrap unavailable")
    try:
        await daemon._begin_cycle()
        with pytest.raises(RuntimeError, match="bootstrap unavailable"):
            await daemon._run_orchestration({"alpha": SimpleNamespace()})
        service.dispatch_card.assert_not_awaited()
    finally:
        await finish_polls(daemon)


@pytest.mark.asyncio
async def test_new_pause_cancels_owned_main_before_any_new_side_writer():
    daemon, service = side_daemon()
    session = candidate("card")
    session.update(phase="monitoring_performer", agent_dispatch={"session_id": "main-owned", "performer_id": "worker"})
    main = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=False))
    daemon.state.update(active_sessions={"card": session}, performer_services_by_id={"worker": main},
                        board_snapshot={"IN_PROGRESS": ["card"]},
                        github_service=SimpleNamespace(poll_board=AsyncMock(return_value={"snapshot": {"TODO": ["card"]}})))
    try:
        await daemon._begin_cycle()
        await daemon._run_orchestration({})
        main.stop_session_confirmed.assert_awaited_once_with("main-owned")
        service.dispatch_card.assert_not_awaited()
        assert session["board_paused"] and session["agent_dispatch"]["session_id"] == "main-owned"
    finally:
        await finish_polls(daemon)


@pytest.mark.asyncio
async def test_existing_side_poll_continues_during_board_outage(monkeypatch):
    daemon, service = side_daemon()
    session = candidate("card")
    session["documenting_side"] = {"status": "running", "session_id": "existing", "blueprint_hash": "bp-card"}
    daemon.state.update(active_sessions={"card": session}, board_snapshot={"IN_PROGRESS": ["card"]},
                        github_service=SimpleNamespace(poll_board=AsyncMock(side_effect=RuntimeError("offline"))))
    monkeypatch.setattr("coordinare.daemon.recover_documenting_side_session", AsyncMock(return_value=True))
    try:
        await daemon._begin_cycle()
        await finish_polls(daemon)
        await daemon._run_orchestration({})
        service.check_status.assert_awaited_once_with("existing")
        service.dispatch_card.assert_not_awaited()
        assert session["documenting_side"]["status"] == "done"
    finally:
        await finish_polls(daemon)

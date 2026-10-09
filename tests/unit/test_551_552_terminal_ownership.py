from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.config import PerformerEndpointConfig
from coordinare.daemon import CoordinareDaemon, _compute_session_eligibilities
from coordinare.graph.nodes.check_board import _reset_and_rehydrate
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services.docker_executor import ContainerInfo
from coordinare.services.documenting_side import poll_to_completion
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.reconciliation import collect_active_session_ids
from coordinare.session import create_session_from_card, session_to_state
from coordinare.state_store import WorkflowSnapshot


def worker(confirmed=True):
    return SimpleNamespace(
        _config=SimpleNamespace(mode="persistent"),
        stop_session_confirmed=AsyncMock(side_effect=RuntimeError("unavailable") if confirmed is None else None,
                                        return_value=confirmed),
        release_session=AsyncMock(),
    )


def ownership_state(*, main_owned=True):
    state = initial_state()
    card = {"id": "card", "issue_id": "issue", "status": "IN_REVIEW",
            "pr_node_id": "PR1", "pr_url": "https://github.com/acme/repo/pull/1"}
    session = create_session_from_card(card)
    session.update(phase="monitoring_pr", performer_stage="reviewing",
                   agent_dispatch={"session_id": "main", "performer_id": "main-worker"} if main_owned else {},
                   documenting_side={"blueprint_hash": "bp", "status": "running", "writer_active": True,
                                     "session_id": "side", "job_id": "side-job"})
    sibling = create_session_from_card({"id": "sibling", "status": "TODO"})
    sibling["phase"] = "dispatching"
    state.update(active_sessions={"card": session, "sibling": sibling}, active_card_id="card")
    session_to_state(session, state)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("focused", [True, False])
@pytest.mark.parametrize("main_owned", [True, False])
async def test_actual_startup_retains_paused_done_workers_until_owned_cleanup(focused, main_owned):
    state = ownership_state(main_owned=main_owned)
    state["active_sessions"]["card"].update(board_paused=True, board_pause_column="BACKLOG", phase="blocked")
    main, side = worker(False), worker(False)
    original = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    original.state.update(state)
    if not focused:
        original.state["active_card_id"] = "sibling"
        session_to_state(original.state["active_sessions"]["sibling"], original.state)
    snapshot = WorkflowSnapshot.model_validate_json(original._build_snapshot().model_dump_json())
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    daemon._state_store = SimpleNamespace(load=AsyncMock(return_value=snapshot))
    github = SimpleNamespace(project_id=1, poll_board=AsyncMock(return_value={
        "snapshot": {"DONE": ["card"], "TODO": ["sibling"]},
    }))
    daemon.state.update(github_service=github, performer_services_by_id={"main-worker": main},
                        performer_services={"documenting": side})
    await daemon._startup_load_snapshot()
    assert "card" in daemon.state["active_sessions"]
    retained = daemon.state["active_sessions"]["card"]
    assert retained["phase"] == "monitoring_performer"
    assert retained["documenting_side"]["session_id"] == "side"
    assert collect_active_session_ids(daemon.state) >= ({"main", "side"} if main_owned else {"side"})
    await daemon._startup_reconciliation_pass()
    assert "card" in daemon.state["active_sessions"]
    daemon.state["board_snapshot"] = {"DONE": ["card"], "TODO": ["sibling"]}
    await daemon._reconcile_board_pauses()
    await daemon._post_cycle_invariants()
    assert "card" in daemon.state["active_sessions"]
    main.stop_session_confirmed.return_value = True
    side.stop_session_confirmed.return_value = True
    await daemon._reconcile_board_pauses()
    await daemon._post_cycle_invariants()
    assert "card" not in daemon.state["active_sessions"]
    side.stop_session_confirmed.assert_any_await("side")
    side.release_session.assert_not_awaited()
    main.release_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False, None])
@pytest.mark.parametrize("main_owned", [True, False])
async def test_closed_pr_confirms_all_writers_or_retains_ownership_and_capacity(confirmed, main_owned):
    state = ownership_state(main_owned=main_owned)
    main, side = worker(True), worker(confirmed)
    github = SimpleNamespace(get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}),
                             move_card=AsyncMock())
    state.update(github_service=github, performer_services_by_id={"main-worker": main},
                 performer_services={"documenting": side}, _pipeline_selected={"card"})
    await monitor_pr(state)
    side.stop_session_confirmed.assert_awaited_once_with("side")
    assert main.stop_session_confirmed.await_count == int(main_owned)
    assert state["current_card"]["status"] == "BLOCKED"
    assert "closed without merging" in state["system_error_reason"]
    assert state["agent_dispatch"] == {}
    assert state["documenting_side"]["writer_active"] is (confirmed is not True)
    assert state["phase"] == ("blocked" if confirmed is True else "monitoring_performer")
    assert state["board_paused"] is (confirmed is not True)
    eligibility = _compute_session_eligibilities(state, state["active_sessions"], 1)
    assert eligibility["sibling"].eligible is (confirmed is True)


@pytest.mark.asyncio
async def test_closed_pr_restart_retries_owned_stop_and_requires_reopen_plus_explicit_todo():
    state = ownership_state(main_owned=False)
    side = worker(False)
    github = SimpleNamespace(get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}),
                             move_card=AsyncMock())
    state.update(github_service=github, performer_services={"documenting": side})
    await monitor_pr(state)
    original = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    original.state.update(state)
    snapshot = WorkflowSnapshot.model_validate_json(original._build_snapshot().model_dump_json())
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    daemon.state.update(github_service=github, performer_services={"documenting": side})
    daemon._restore_from_snapshot(snapshot)
    session = daemon.state["active_sessions"]["card"]
    assert session["board_paused"] and session["documenting_side"]["session_id"] == "side"
    github.get_pr_review_context.return_value["state"] = "OPEN"
    daemon.state["board_snapshot"] = {"TODO": ["card", "sibling"]}
    await daemon._reconcile_board_pauses()
    await _reset_and_rehydrate(daemon.state, {}, ["card"], 1, daemon.state["active_sessions"])
    assert session["board_paused"] and session["phase"] == "monitoring_performer"
    side.stop_session_confirmed.return_value = True
    await daemon._reconcile_board_pauses(board_is_fresh=False)
    assert session["board_paused"] and session["phase"] == "blocked"
    await daemon._reconcile_board_pauses()
    await _reset_and_rehydrate(daemon.state, {}, ["card"], 1, daemon.state["active_sessions"])
    assert not session["board_paused"]
    assert session["phase"] == "monitoring_pr"
    assert session["current_card"]["pr_node_id"] == "PR1"
    assert not session["system_error_reason"]


@pytest.mark.asyncio
async def test_closed_pr_keeps_unknown_main_owner_after_side_stop_is_confirmed():
    state = ownership_state()
    main, side = worker(False), worker(True)
    github = SimpleNamespace(get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}),
                             move_card=AsyncMock())
    state.update(github_service=github, performer_services_by_id={"main-worker": main},
                 performer_services={"documenting": side})
    await monitor_pr(state)
    assert state["agent_dispatch"]["session_id"] == "main"
    assert state["documenting_side"]["session_id"] is None
    assert state["board_paused"] and state["phase"] == "monitoring_performer"
    eligibility = _compute_session_eligibilities(state, state["active_sessions"], 1)
    assert not eligibility["sibling"].eligible


@pytest.mark.asyncio
async def test_closed_pr_missing_stop_service_retains_owned_side():
    state = ownership_state(main_owned=False)
    state["github_service"] = SimpleNamespace(
        get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}), move_card=AsyncMock(),
    )
    await monitor_pr(state)
    assert state["board_paused"] and state["documenting_side"]["writer_active"]
    assert state["documenting_side"]["session_id"] == "side"
    assert not _compute_session_eligibilities(state, state["active_sessions"], 1)["sibling"].eligible


@pytest.mark.asyncio
@pytest.mark.parametrize("late_response", ["error", "done", "running"])
async def test_closed_pr_cancellation_freezes_canonical_side_and_supersedes_late_poll(late_response):
    state = ownership_state(main_owned=False)
    session = state["active_sessions"]["card"]
    started, response = asyncio.Event(), asyncio.Event()

    async def check_status(_):
        started.set()
        await response.wait()
        if late_response == "error":
            raise RuntimeError("job already removed")
        return {"status": "docs_committed" if late_response == "done" else "running", "head_sha": "late"}

    async def stop(_):
        assert session["board_paused"]
        assert "closed without merging" in session["system_error_reason"]
        return True

    state.update(github_service=SimpleNamespace(
        get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}), move_card=AsyncMock()),
        performer_services={"documenting": SimpleNamespace(stop_session_confirmed=stop)})
    poll = asyncio.create_task(poll_to_completion(session, check_status, session_id="side", get_session=lambda: session))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    try:
        await monitor_pr(state)
    finally:
        response.set()
    assert await asyncio.wait_for(poll, timeout=1.0) == "superseded"
    assert not session["documenting_side"]["writer_active"]
    assert session["documenting_side"]["session_id"] is None
    assert state["phase"] == "blocked"


@pytest.mark.asyncio
async def test_merged_pr_does_not_cancel_parallel_documenter(monkeypatch):
    state = ownership_state(main_owned=False)
    side = worker(True)
    state.update(github_service=SimpleNamespace(
        get_pr_review_context=AsyncMock(return_value={"state": "MERGED", "reviews": []}), move_card=AsyncMock()),
        performer_services={"documenting": side})
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", AsyncMock(return_value=({}, False)))
    await monitor_pr(state)
    side.stop_session_confirmed.assert_not_awaited()
    assert state["documenting_side"]["writer_active"]
    assert not state["board_paused"]


@pytest.mark.asyncio
async def test_closed_pr_prepopulated_different_pr_cannot_resume_on_todo():
    state = ownership_state(main_owned=False)
    state["documenting_side"]["writer_active"] = False
    state["documenting_side"]["status"] = "done"
    github = SimpleNamespace(get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}),
                             move_card=AsyncMock())
    state["github_service"] = github
    await monitor_pr(state)
    session = state["active_sessions"]["card"]
    session["current_card"].update(pr_node_id="PR2", pr_url="https://github.com/acme/repo/pull/2")
    github.get_pr_review_context.return_value["state"] = "OPEN"
    await _reset_and_rehydrate(state, {}, ["card"], 1, state["active_sessions"])
    assert session["phase"] == "blocked"
    assert "closed without merging" in session["system_error_reason"]


@pytest.mark.asyncio
async def test_cancelled_close_stop_retains_foreground_recovery_identity_before_await():
    state = ownership_state()
    state["documenting_side"].update(status="done", writer_active=False, session_id=None)
    started = asyncio.Event()

    async def stop(_):
        started.set()
        await asyncio.Event().wait()

    state.update(github_service=SimpleNamespace(
        get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}), move_card=AsyncMock()),
        performer_services_by_id={"main-worker": SimpleNamespace(stop_session_confirmed=stop)})
    closing = asyncio.create_task(monitor_pr(state))
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert state["active_sessions"]["card"]["phase"] == "monitoring_performer"
        assert "main" in collect_active_session_ids(state)
    finally:
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing


@pytest.mark.asyncio
@pytest.mark.parametrize("main_owned, side_owned", [(True, False), (False, True), (True, True)])
async def test_real_docker_startup_defers_paused_owners_to_confirmed_stop_path(monkeypatch, main_owned, side_owned):
    state = ownership_state(main_owned=main_owned)
    session = state["active_sessions"]["card"]
    session.update(board_paused=True, board_pause_column="BACKLOG", phase="monitoring_performer")
    if not side_owned:
        session["documenting_side"].update(status="done", writer_active=False, session_id=None)
    main = HTTPPerformerService(PerformerEndpointConfig(
        id="main-worker", roles=["reviewing"], mode="ephemeral", image="example:latest",
    ))
    side = HTTPPerformerService(PerformerEndpointConfig(
        id="documenter", roles=["documenting"], mode="ephemeral", image="example:latest",
    ))
    ids = (["main"] if main_owned else []) + (["side"] if side_owned else [])
    containers = [ContainerInfo(
        container_id=f"container-{sid}", name=sid, image="example:latest", started_at=datetime.now(UTC),
        labels={"coordinare.session_id": sid, "coordinare.spec_version": "076"},
    ) for sid in ids]
    executor = SimpleNamespace(list_containers_by_label=AsyncMock(return_value=containers),
                               probe_healthz=AsyncMock(return_value=False),
                               stop_container=AsyncMock(return_value=False))
    monkeypatch.setattr("coordinare.services.docker_executor.DockerExecutor", lambda: executor)
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    daemon.state.update(state)
    daemon.state.update(board_snapshot={"DONE": ["card"], "TODO": ["sibling"]},
                        performer_services_by_id={"main-worker": main}, performer_services={"documenting": side})
    daemon._reconcile_session_phases(daemon.state["active_sessions"], daemon.state["board_snapshot"], None)
    await daemon._startup_reconciliation_pass()
    assert collect_active_session_ids(daemon.state) >= set(ids)
    assert session["phase"] == "monitoring_performer"
    assert not session.get("reconciled_dispatch_pending")
    executor.stop_container.assert_not_awaited()
    assert not main.has_live_session("main") and not side.has_live_session("side")

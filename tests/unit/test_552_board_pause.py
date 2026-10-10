from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon, _persist_one_session, _restored_session_dict
from coordinare.state_store import WorkflowSnapshot


def daemon_with_worker(column: str, *, stopped: bool = True):
    service = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=stopped))
    session = {
        "current_card": {"id": "card", "status": "IN_PROGRESS"},
        "phase": "monitoring_performer", "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "owned", "performer_id": "worker"},
        "relay_feedback": [{"message": "keep instruction"}],
    }
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    daemon._state.update({
        "active_sessions": {"card": session}, "current_card": session["current_card"],
        "active_card_id": "card", "phase": "monitoring_performer",
        "board_snapshot": {column: ["card"]},
        "performer_services_by_id": {"worker": service},
    })
    return daemon, session, service


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["BACKLOG", "TODO"])
async def test_pause_stops_worker_and_preserves_instructions(column):
    daemon, session, service = daemon_with_worker(column)
    await daemon._reconcile_board_pauses()
    service.stop_session_confirmed.assert_awaited_once_with("owned")
    assert session["agent_dispatch"] == {}
    assert session["board_paused"] is True
    assert session["relay_feedback"] == [{"message": "keep instruction"}]
    assert daemon._state["active_sessions"]["card"] is session


@pytest.mark.asyncio
async def test_uncertain_stop_retains_identity_and_blocks_resume():
    daemon, session, service = daemon_with_worker("BACKLOG", stopped=False)
    await daemon._reconcile_board_pauses()
    daemon._state["board_snapshot"] = {"TODO": ["card"]}
    await daemon._reconcile_board_pauses()
    assert session["agent_dispatch"]["session_id"] == "owned"
    assert session["phase"] == "monitoring_performer"
    assert session["board_paused"] is True
    assert service.stop_session_confirmed.await_count == 2


@pytest.mark.asyncio
async def test_resume_dispatches_once_after_stop():
    daemon, session, service = daemon_with_worker("BACKLOG")
    await daemon._reconcile_board_pauses()
    daemon._state["board_snapshot"] = {"TODO": ["card"]}
    await daemon._reconcile_board_pauses()
    await daemon._reconcile_board_pauses()
    assert session["phase"] == "dispatching"
    assert session["board_paused"] is False
    service.stop_session_confirmed.assert_awaited_once()


def test_pause_and_identity_survive_restart():
    daemon, session, _ = daemon_with_worker("BACKLOG")
    session["board_paused"] = True
    session["phase"] = "blocked"
    persisted = _persist_one_session("card", session)
    restored = _restored_session_dict("card", persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked"), session["current_card"])
    assert restored["board_paused"] is True
    assert restored["agent_dispatch"]["session_id"] == "owned"
    assert restored["relay_feedback"] == session["relay_feedback"]
    daemon._reconcile_session_phases({"card": restored}, {"TODO": ["card"]}, None)
    assert restored["phase"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("still_present", [False, True])
async def test_http_stop_verifies_absence_before_removing_tracking(still_present):
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.http_performer_service import HTTPPerformerService, _EphemeralJob

    runtime = SimpleNamespace(stop=AsyncMock(), find_session=AsyncMock(return_value=object() if still_present else None))
    config = PerformerEndpointConfig.model_validate({
        "id": "worker", "mode": "ephemeral", "roles": ["implementing"], "image": "performer:base",
    })
    service = HTTPPerformerService(config, runtime=runtime)
    client = SimpleNamespace(aclose=AsyncMock())
    job = _EphemeralJob("pod", "http://worker", client, "runner")
    service._active_jobs["owned"] = job
    assert await service.stop_session_confirmed("owned") is (not still_present)
    assert ("owned" in service._active_jobs) is still_present
    runtime.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_pausing_one_card_stops_side_writer_without_touching_sibling():
    daemon, session, service = daemon_with_worker("BACKLOG")
    side_service = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=True))
    session["documenting_side"] = {"status": "running", "session_id": "side", "writer_active": True}
    sibling = {"current_card": {"id": "sibling"}, "phase": "monitoring_performer", "agent_dispatch": {"session_id": "sibling-worker"}}
    daemon._state["active_sessions"]["sibling"] = sibling
    daemon._state["board_snapshot"]["IN_PROGRESS"] = ["sibling"]
    daemon._state["performer_services"] = {"documenting": side_service}
    await daemon._reconcile_board_pauses()
    side_service.stop_session_confirmed.assert_awaited_once_with("side")
    assert session["documenting_side"]["writer_active"] is False
    assert sibling["agent_dispatch"]["session_id"] == "sibling-worker"
    service.stop_session_confirmed.assert_awaited_once_with("owned")


@pytest.mark.asyncio
async def test_paused_fallback_does_not_dispatch_or_drop_worker():
    from coordinare.daemon import _all_ineligible_fallback

    daemon, session, _ = daemon_with_worker("BACKLOG", stopped=False)
    await daemon._reconcile_board_pauses()
    graph = AsyncMock()
    await _all_ineligible_fallback(daemon._state, graph, daemon._state["active_sessions"])
    graph.ainvoke.assert_not_awaited()
    assert session["agent_dispatch"]["session_id"] == "owned"


def test_paused_card_cannot_start_side_writer(monkeypatch):
    from coordinare.services import documenting_side

    monkeypatch.setattr(documenting_side, "documenter_side_run_wanted", lambda _: True)
    assert documenting_side.should_dispatch({"board_paused": True, "phase": "monitoring_performer", "performer_stage": "implementing"})[0] is False


@pytest.mark.asyncio
async def test_http_stop_restores_handle_after_restart():
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.http_performer_service import HTTPPerformerService

    runtime = SimpleNamespace(stop=AsyncMock(), find_session=AsyncMock(side_effect=[SimpleNamespace(handle="restored-pod"), None]))
    config = PerformerEndpointConfig.model_validate({"id": "worker", "mode": "ephemeral", "roles": ["implementing"], "image": "performer:base"})
    service = HTTPPerformerService(config, runtime=runtime)
    assert await service.stop_session_confirmed("owned") is True
    runtime.stop.assert_awaited_once_with("restored-pod")


@pytest.mark.asyncio
async def test_http_persistent_cancel_requires_terminal_job():
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.http_performer_service import HTTPPerformerService

    client = SimpleNamespace(cancel_job=AsyncMock(), get_job=AsyncMock(return_value=SimpleNamespace(state="running")))
    config = PerformerEndpointConfig.model_validate({"id": "worker", "mode": "persistent", "roles": ["implementing"], "endpoint": "http://worker", "image": "performer:base"})
    service = HTTPPerformerService(config, client=client)
    assert await service.stop_session_confirmed("runner") is False
    client.get_job.return_value = SimpleNamespace(state="cancelled")
    assert await service.stop_session_confirmed("runner") is True


@pytest.mark.asyncio
async def test_stop_exception_retains_worker_identity():
    daemon, session, service = daemon_with_worker("BACKLOG")
    service.stop_session_confirmed.side_effect = RuntimeError("runtime unavailable")
    await daemon._reconcile_board_pauses()
    assert session["board_paused"] is True
    assert session["agent_dispatch"]["session_id"] == "owned"
    assert session["phase"] == "monitoring_performer"


def test_current_pause_snapshot_satisfies_json_contract():
    import json
    from pathlib import Path

    import jsonschema

    _, session, _ = daemon_with_worker("BACKLOG")
    session["board_paused"] = True
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="monitoring_performer", active_sessions={"card": _persist_one_session("card", session)})
    schema = json.loads((Path(__file__).resolve().parents[2] / "specs/003-state-persistence/contracts/workflow-snapshot.schema.json").read_text())
    jsonschema.validate(snapshot.model_dump(mode="json"), schema)


@pytest.mark.asyncio
async def test_unconfirmed_pause_is_visible_in_activity_feed():
    from coordinare.services.activity_log import ActivityLog

    daemon, _, _ = daemon_with_worker("BACKLOG", stopped=False)
    activity = ActivityLog()
    daemon._state["activity_log"] = activity
    await daemon._reconcile_board_pauses()
    assert "may still be running" in activity.snapshot()[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("still_present", [False, True])
async def test_docker_stop_requires_running_container_absence(monkeypatch, still_present):
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.docker_executor import DockerExecutor
    from coordinare.services.docker_runtime import DockerRuntime
    from coordinare.services.http_performer_service import HTTPPerformerService, _EphemeralJob

    runtime = DockerRuntime()
    monkeypatch.setattr(runtime, "stop", AsyncMock())
    listed = AsyncMock(side_effect=[[SimpleNamespace(container_id="container")], [SimpleNamespace(container_id="container")] if still_present else []])
    monkeypatch.setattr(DockerExecutor, "list_containers_by_label", listed)
    config = PerformerEndpointConfig.model_validate({"id": "worker", "mode": "ephemeral", "roles": ["implementing"], "image": "performer:base"})
    service = HTTPPerformerService(config, runtime=runtime)
    service._active_jobs["owned"] = _EphemeralJob("container", "http://worker", SimpleNamespace(aclose=AsyncMock()), "runner")
    assert await service.stop_session_confirmed("owned") is (not still_present)
    assert ("owned" in service._active_jobs) is still_present


@pytest.mark.asyncio
async def test_uncertain_pause_crosses_actual_snapshot_save_gate():
    daemon, _, _ = daemon_with_worker("BACKLOG", stopped=False)
    store = SimpleNamespace(save=AsyncMock())
    daemon._state_store = store
    signature = daemon._lifecycle_signature()
    await daemon._reconcile_board_pauses()
    signature = await daemon._save_snapshot_if_changed(signature)
    store.save.assert_awaited_once()
    persisted = store.save.await_args.args[0].active_sessions["card"]
    assert persisted.board_paused is True
    assert persisted.agent_session_id == "owned"
    await daemon._save_snapshot_if_changed(signature)
    store.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_todo_does_not_resume_without_operator_transition():
    daemon, session, service = daemon_with_worker("TODO")
    await daemon._reconcile_board_pauses()
    await daemon._reconcile_board_pauses()
    assert session["board_paused"] is True
    assert session["phase"] == "blocked"
    service.stop_session_confirmed.assert_awaited_once()
    daemon._state["board_snapshot"] = {"BACKLOG": ["card"]}
    await daemon._reconcile_board_pauses()
    daemon._state["board_snapshot"] = {"TODO": ["card"]}
    await daemon._reconcile_board_pauses()
    assert session["board_paused"] is False
    assert session["phase"] == "dispatching"


def test_startup_new_backlog_keeps_live_identity_in_reconciliation_set():
    from coordinare.services.reconciliation import _collect_in_flight_sessions

    daemon, session, _ = daemon_with_worker("BACKLOG")
    daemon._reconcile_session_phases({"card": session}, {"BACKLOG": ["card"]}, None)
    assert session["phase"] == "monitoring_performer"
    assert session["board_paused"] is True
    assert _collect_in_flight_sessions(daemon._state)["card"]["agent_dispatch"]["session_id"] == "owned"


@pytest.mark.parametrize("column", ["BACKLOG", "TODO"])
@pytest.mark.parametrize("status, writer_active", [("running", False), ("failed", True)])
def test_startup_side_only_writer_preserves_pause_ownership(column, status, writer_active):
    daemon, session, _ = daemon_with_worker(column)
    session.update(phase="monitoring_pr", agent_dispatch={}, documenting_side={
        "session_id": "side", "status": status, "writer_active": writer_active,
    })
    daemon._reconcile_session_phases({"card": session}, {column: ["card"]}, None)
    assert session["board_paused"] is True
    assert session["board_pause_column"] == column
    assert session["phase"] == "monitoring_performer"
    assert session["documenting_side"]["session_id"] == "side"


@pytest.mark.asyncio
async def test_paused_fallback_runs_real_board_pickup_for_new_sibling():
    from coordinare.daemon import _all_ineligible_fallback

    daemon, _, _ = daemon_with_worker("BACKLOG", stopped=False)
    await daemon._reconcile_board_pauses()
    board = {
        "snapshot": {"BACKLOG": ["card"], "TODO": ["new-card"], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": []},
        "titles": {"card": "Paused card", "new-card": "Sibling"}, "descriptions": {}, "issue_numbers": {},
    }
    github = SimpleNamespace(poll_board=AsyncMock(return_value=board))
    daemon._state["github_service"] = github
    daemon._state["config"] = SimpleNamespace(max_concurrent_cards=2)
    graph = AsyncMock()
    state = await _all_ineligible_fallback(daemon._state, graph, daemon._state["active_sessions"])
    assert "new-card" in state["active_sessions"]
    assert state["active_sessions"]["card"]["agent_dispatch"]["session_id"] == "owned"
    assert state["active_sessions"]["card"]["board_paused"] is True
    graph.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_maintenance_never_reconciles_live_identity_as_stale(monkeypatch):
    from coordinare.daemon import _all_ineligible_fallback
    from coordinare.services import reconciliation

    daemon, session, service = daemon_with_worker("BACKLOG", stopped=False)
    await daemon._reconcile_board_pauses()
    service.has_live_session = lambda _: False
    daemon._state["performer_services"] = {"implementing": service}
    daemon._state["github_service"] = SimpleNamespace(poll_board=AsyncMock(return_value={
        "snapshot": {"BACKLOG": ["card"], "TODO": ["new-card"]},
        "titles": {"new-card": "Sibling"}, "descriptions": {}, "issue_numbers": {},
    }))
    daemon._state["config"] = SimpleNamespace(max_concurrent_cards=2)
    reconcile = AsyncMock()
    monkeypatch.setattr(reconciliation, "handle_potentially_stale_session", reconcile)
    await _all_ineligible_fallback(daemon._state, AsyncMock(), daemon._state["active_sessions"])
    reconcile.assert_not_awaited()
    assert session["agent_dispatch"]["session_id"] == "owned"


@pytest.mark.asyncio
async def test_real_startup_board_read_preserves_pause_worker_for_runtime_recovery():
    from coordinare.services.reconciliation import _collect_in_flight_sessions

    daemon, session, _ = daemon_with_worker("BACKLOG")
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="monitoring_performer", active_card_id="card", active_sessions={"card": _persist_one_session("card", session)})
    daemon._state["github_service"] = SimpleNamespace(project_id=1, poll_board=AsyncMock(return_value={"snapshot": {"BACKLOG": ["card"]}}))
    await daemon._reconcile_with_board(snapshot)
    assert daemon._state["phase"] == "monitoring_performer"
    assert session["board_paused"] is True
    assert _collect_in_flight_sessions(daemon._state)["card"]["agent_dispatch"]["session_id"] == "owned"


@pytest.mark.asyncio
async def test_todo_pause_transition_survives_restart_and_still_requires_resume():
    daemon, session, _ = daemon_with_worker("TODO")
    await daemon._reconcile_board_pauses()
    persisted = _persist_one_session("card", session)
    assert persisted.board_pause_column == "TODO"
    restored = _restored_session_dict("card", persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked"), session["current_card"])
    daemon._state["active_sessions"] = {"card": restored}
    daemon._reconcile_session_phases({"card": restored}, {"TODO": ["card"]}, None)
    await daemon._reconcile_board_pauses()
    assert restored["board_paused"] is True
    assert restored["phase"] == "blocked"
    daemon._state["board_snapshot"] = {"BACKLOG": ["card"]}
    await daemon._reconcile_board_pauses()
    daemon._state["board_snapshot"] = {"TODO": ["card"]}
    await daemon._reconcile_board_pauses()
    assert restored["board_paused"] is False
    assert restored["relay_feedback"] == [{"message": "keep instruction"}]


@pytest.mark.asyncio
async def test_board_maintenance_rebases_only_unpaused_siblings(monkeypatch):
    import importlib

    from coordinare.services import rebase

    check_board_module = importlib.import_module("coordinare.graph.nodes.check_board")
    daemon, session, _ = daemon_with_worker("BACKLOG")
    await daemon._reconcile_board_pauses()
    sibling = {"phase": "monitoring_pr", "current_card": {"id": "sibling"}}
    daemon._state["active_sessions"]["sibling"] = sibling
    daemon._state["last_known_main_sha"] = "old-main"
    github = SimpleNamespace(_current_token=AsyncMock(return_value="test-token"))
    monkeypatch.setattr(check_board_module, "repo_url_from_config", lambda _: "https://github.com/example/repository")
    monkeypatch.setattr(rebase, "fetch_main_sha", AsyncMock(return_value="new-main"))
    run_edge = AsyncMock()
    monkeypatch.setattr(check_board_module, "_run_edge_rebase_round", run_edge)
    await check_board_module._maybe_rebase_on_main_change(daemon._state, github, SimpleNamespace(), 2)
    assert run_edge.await_args.args[2] == {"sibling": sibling}
    assert session["board_paused"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("already_paused,column", [(True, "BACKLOG"), (False, "BACKLOG"), (False, "TODO")])
async def test_actual_daemon_preflight_cannot_rebase_paused_or_newly_paused_cards(monkeypatch, already_paused, column):
    import coordinare.daemon as daemon_module
    from coordinare.models.rebase import RebaseJob, RebaseOutcome
    from coordinare.services import rebase

    daemon, session, _ = daemon_with_worker(column)
    session.update(
        board_paused=already_paused, phase="blocked", workspace_branch="coordinare/card/work",
        relay_feedback=[{"message": "Human instruction survives"}],
    )
    session["current_card"]["pr_url"] = "https://github.com/example/repository/pull/123"
    if already_paused:
        session["agent_dispatch"] = {}  # confirmed cancellation already released the worker
    session["dispatched_feedback"] = {"stage": "implementing", "items": [{"message": "In-flight instruction"}]}
    before = dict(session["agent_dispatch"])
    github = SimpleNamespace(
        poll_board=AsyncMock(return_value={"snapshot": {column: ["card"]}}),
        _current_token=AsyncMock(return_value="test-token"),
    )
    daemon._state["github_service"] = github
    daemon._state["config"] = SimpleNamespace()
    daemon._state["last_known_main_sha"] = "old-main"
    monkeypatch.setattr(daemon_module, "repo_url_from_config", lambda _: "https://github.com/example/repository")
    monkeypatch.setattr(daemon_module, "fetch_main_sha", AsyncMock(return_value="new-main"))
    rebase_branch = AsyncMock(return_value=RebaseJob(card_id="card", branch="coordinare/card/work", target_main_sha="new-main", outcome=RebaseOutcome.BLOCKED, conflicted_files=["file.py"]))
    monkeypatch.setattr(rebase, "rebase_branch", rebase_branch)
    await daemon_module._preflight_poll_board(daemon._state, github, daemon._state["active_sessions"])
    rebase_branch.assert_not_awaited()
    assert session["phase"] == "blocked"
    assert session["agent_dispatch"] == before
    assert session["relay_feedback"] == [{"message": "Human instruction survives"}]
    assert session["dispatched_feedback"] == {"stage": "implementing", "items": [{"message": "In-flight instruction"}]}


@pytest.mark.asyncio
async def test_shared_rebase_round_ignores_paused_card_without_board_snapshot(monkeypatch):
    from coordinare.services import rebase

    _, session, _ = daemon_with_worker("BACKLOG")
    session.update(board_paused=True, phase="blocked", workspace_branch="coordinare/card/work")
    session["current_card"]["pr_url"] = "https://github.com/example/repository/pull/123"
    rebasing = AsyncMock()
    monkeypatch.setattr(rebase, "rebase_branch", rebasing)
    assert rebase.detect_stale_branches({"card": session}, "new-main") == []
    round_result = await rebase.run_rebase_round({"card": session}, "new-main", "https://github.com/example/repository", "test-token")
    assert round_result.jobs == []
    rebasing.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_preflight_rebases_active_sibling_only(monkeypatch):
    import coordinare.daemon as daemon_module
    from coordinare.models.rebase import RebaseJob, RebaseOutcome
    from coordinare.services import rebase

    daemon, paused, _ = daemon_with_worker("BACKLOG")
    await daemon._reconcile_board_pauses()
    paused["workspace_branch"] = "coordinare/card/work"
    paused["current_card"]["pr_url"] = "https://github.com/example/repository/pull/123"
    sibling = {"phase": "monitoring_pr", "workspace_branch": "coordinare/sibling/work", "current_card": {"id": "sibling", "pr_node_id": "PR456", "pr_url": "https://github.com/example/repository/pull/456"}}
    daemon._state["active_sessions"]["sibling"] = sibling
    github = SimpleNamespace(poll_board=AsyncMock(return_value={"snapshot": {"BACKLOG": ["card"], "IN_REVIEW": ["sibling"]}}), _current_token=AsyncMock(return_value="test-token"), get_pr_review_context=AsyncMock(return_value={"state": "OPEN"}))
    daemon._state["config"] = SimpleNamespace()
    daemon._state["last_known_main_sha"] = "old-main"
    monkeypatch.setattr(daemon_module, "repo_url_from_config", lambda _: "https://github.com/example/repository")
    monkeypatch.setattr(daemon_module, "fetch_main_sha", AsyncMock(return_value="new-main"))
    rebasing = AsyncMock(return_value=RebaseJob(card_id="sibling", branch="coordinare/sibling/work", target_main_sha="new-main", outcome=RebaseOutcome.SKIPPED))
    monkeypatch.setattr(rebase, "rebase_branch", rebasing)
    await daemon_module._preflight_poll_board(daemon._state, github, daemon._state["active_sessions"])
    rebasing.assert_awaited_once_with("https://github.com/example/repository", "coordinare/sibling/work", "new-main", "test-token")
    assert paused["board_paused"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_stopped", [True, False])
async def test_closed_pr_pause_resume_preserves_reopen_guard(initially_stopped):
    from coordinare.services.closed_pr import CLOSED_PR_REASON_PREFIX

    daemon, session, service = daemon_with_worker("BACKLOG", stopped=initially_stopped)
    reason = f"{CLOSED_PR_REASON_PREFIX} https://github.com/example/repository/pull/123"
    session.update(phase="blocked", system_error_reason=reason)
    session["current_card"].update(status="BLOCKED", pr_node_id="PR123")
    await daemon._reconcile_board_pauses()
    assert session["system_error_reason"] == reason
    service.stop_session_confirmed.return_value = True
    daemon._state["board_snapshot"] = {"TODO": ["card"]}
    await daemon._reconcile_board_pauses()
    assert session["system_error_reason"] == reason
    assert session["phase"] == "blocked"
    assert not session.get("reconciled_dispatch_pending")


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_column", ["TODO", "BACKLOG"])
async def test_post_cycle_cached_board_does_not_cancel_successful_dispatch(tmp_path, cached_column):
    from coordinare.services.fake_github import FakeGitHubService

    daemon, session, service = daemon_with_worker(cached_column)
    github = FakeGitHubService(bare_repo_path=tmp_path, human_reviewers=["alice"])
    github.seed_card("card", title="Fresh dispatch", body="", issue_number=1, status=cached_column)
    daemon._state["github_service"] = github
    daemon._state["board_snapshot"] = (await github.poll_board())["snapshot"]
    await github.move_card("card", "IN_PROGRESS")
    assert "card" in (await github.poll_board())["snapshot"]["IN_PROGRESS"]
    await daemon._reconcile_board_state_with_release()
    service.stop_session_confirmed.assert_not_awaited()
    assert not session.get("board_paused")
    assert session["agent_dispatch"]["session_id"] == "owned"
    # A subsequent actual human pause is authoritative at the next fresh read.
    await github.move_card("card", "BACKLOG")
    daemon._state["board_snapshot"] = (await github.poll_board())["snapshot"]
    await daemon._reconcile_board_pauses()
    service.stop_session_confirmed.assert_awaited_once_with("owned")
    assert session["board_paused"] is True
    assert session["agent_dispatch"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('foreground_active', [True, False])
@pytest.mark.parametrize('startup_pause', [True, False])
async def test_pause_origin_survives_main_stop_side_uncertainty_and_json_restart(foreground_active, startup_pause):
    from coordinare.state_store import PersistedSession

    daemon, session, main_service = daemon_with_worker('BACKLOG')
    if not foreground_active:
        session.update(phase='monitoring_pr', agent_dispatch={})
        session['current_card']['status'] = 'IN_REVIEW'
    side_service = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=False))
    session['documenting_side'] = {'status': 'running', 'session_id': 'side-owned', 'writer_active': True}
    daemon._state['performer_services'] = {'documenting': side_service}
    if startup_pause:
        daemon._reconcile_session_phases({'card': session}, {'BACKLOG': ['card']}, None)
    await daemon._reconcile_board_pauses()
    assert session['board_paused'] is True
    assert session['agent_dispatch'] == {}
    assert session['documenting_side']['writer_active'] is True
    persisted = _persist_one_session('card', session)
    persisted = PersistedSession.model_validate_json(persisted.model_dump_json())
    restored = _restored_session_dict('card', persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase='monitoring_performer'), session['current_card'])
    daemon._state['active_sessions'] = {'card': restored}
    daemon._reconcile_session_phases({'card': restored}, {'BACKLOG': ['card']}, None)
    side_service.stop_session_confirmed.return_value = True
    daemon._state['board_snapshot'] = {'TODO': ['card']}
    await daemon._reconcile_board_pauses()
    assert restored['board_paused'] is False
    assert restored['phase'] == ('dispatching' if foreground_active else 'monitoring_pr')
    assert bool(restored.get('reconciled_dispatch_pending')) is foreground_active
    assert restored['relay_feedback'] == session['relay_feedback']
    assert main_service.stop_session_confirmed.await_count == int(foreground_active)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,identity,expected", [
    ("monitoring_pr", {"session_id": "owned", "performer_id": "worker"}, "monitoring_pr"),
    ("dispatching", {}, "dispatching"),
    ("monitoring_performer", {}, "dispatching"),
])
async def test_pause_resumes_foreground_phase_not_retained_session_identity(phase, identity, expected):
    daemon, session, _ = daemon_with_worker("BACKLOG")
    session.update(phase=phase, agent_dispatch=identity)
    side_service = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=True))
    session["documenting_side"] = {"status": "running", "session_id": "side-owned", "writer_active": True}
    daemon._state["performer_services"] = {"documenting": side_service}
    await daemon._reconcile_board_pauses()
    daemon._state["board_snapshot"] = {"TODO": ["card"]}
    await daemon._reconcile_board_pauses()
    assert session["phase"] == expected
    assert bool(session.get("reconciled_dispatch_pending")) is (expected == "dispatching")
    assert session["board_pause_resume_phase"] == ""


@pytest.mark.asyncio
async def test_pause_resume_phase_change_crosses_save_gate_and_legacy_hydration_clears_it():
    from coordinare.session import session_to_state
    from coordinare.state_store import PersistedSession

    daemon, session, _ = daemon_with_worker("BACKLOG")
    session.update(board_paused=True, board_pause_column="BACKLOG", board_pause_resume_phase="dispatching")
    store = SimpleNamespace(save=AsyncMock())
    daemon._state_store = store
    signature = daemon._lifecycle_signature()
    session["board_pause_resume_phase"] = "monitoring_pr"
    signature = await daemon._save_snapshot_if_changed(signature)
    store.save.assert_awaited_once()
    assert store.save.await_args.args[0].active_sessions["card"].board_pause_resume_phase == "monitoring_pr"
    await daemon._save_snapshot_if_changed(signature)
    store.save.assert_awaited_once()
    flat = {"board_pause_resume_phase": "monitoring_pr"}
    session_to_state({}, flat)
    assert flat["board_pause_resume_phase"] == ""
    assert PersistedSession(card_id="legacy").board_pause_resume_phase == ""


@pytest.mark.asyncio
@pytest.mark.parametrize('read_failure', ['failed', 'deferred', 'missing_snapshot', 'malformed_snapshot', 'malformed_columns'])
async def test_actual_invoke_does_not_infer_new_pause_from_stale_board(monkeypatch, read_failure):
    import coordinare.daemon as daemon_module

    daemon, session, service = daemon_with_worker('TODO')
    github = SimpleNamespace(poll_board=AsyncMock(side_effect=RuntimeError('board unavailable')))
    if read_failure == 'missing_snapshot':
        github.poll_board.side_effect = None
        github.poll_board.return_value = {'titles': {}}
    elif read_failure == 'malformed_snapshot':
        github.poll_board.side_effect = None
        github.poll_board.return_value = {'snapshot': ['TODO']}
    elif read_failure == 'malformed_columns':
        github.poll_board.side_effect = None
        github.poll_board.return_value = {'snapshot': {'TODO': 'card'}}
    elif read_failure == 'deferred':
        monkeypatch.setattr(daemon_module, 'github_operation_ready', lambda *_: (False, 30))
    daemon._state['github_service'] = github
    daemon._state['config'] = SimpleNamespace(max_concurrent_cards=1)
    async def preserve_state(state):
        return state
    monkeypatch.setattr(daemon_module, '_all_ineligible_fallback', AsyncMock(side_effect=lambda state, *_: state))
    daemon._graph.ainvoke = AsyncMock(side_effect=preserve_state)
    await daemon._invoke_multi_session()
    service.stop_session_confirmed.assert_not_awaited()
    assert not session.get('board_paused')
    assert session['agent_dispatch']['session_id'] == 'owned'


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed', [True, False])
async def test_actual_invoke_retries_existing_pause_stop_during_outage_without_stale_resume(monkeypatch, confirmed):
    import coordinare.daemon as daemon_module

    daemon, session, service = daemon_with_worker('IN_PROGRESS', stopped=confirmed)
    session.update(board_paused=True, board_pause_column='BACKLOG', board_pause_resume_phase='dispatching')
    daemon._state['github_service'] = SimpleNamespace(poll_board=AsyncMock(side_effect=RuntimeError('board unavailable')))
    daemon._state['config'] = SimpleNamespace(max_concurrent_cards=1)
    async def preserve_state(state):
        return state
    daemon._graph.ainvoke = AsyncMock(side_effect=preserve_state)
    monkeypatch.setattr(daemon_module, '_all_ineligible_fallback', AsyncMock(side_effect=lambda state, *_: state))
    await daemon._invoke_multi_session()
    service.stop_session_confirmed.assert_awaited_once_with('owned')
    assert session['board_paused'] is True
    assert session['board_pause_column'] == 'BACKLOG'
    assert session['board_pause_resume_phase'] == 'dispatching'
    assert session['phase'] == ('blocked' if confirmed else 'monitoring_performer')


@pytest.mark.asyncio
async def test_actual_invoke_successful_fresh_board_confirms_pause_without_extra_poll(monkeypatch):
    import coordinare.daemon as daemon_module

    daemon, session, service = daemon_with_worker("IN_PROGRESS")
    github = SimpleNamespace(poll_board=AsyncMock(return_value={"snapshot": {"BACKLOG": ["card"]}}))
    daemon._state["github_service"] = github
    daemon._state["config"] = SimpleNamespace(max_concurrent_cards=1)
    monkeypatch.setattr(daemon_module, "_all_ineligible_fallback", AsyncMock(side_effect=lambda state, *_: state))
    await daemon._invoke_multi_session()
    service.stop_session_confirmed.assert_awaited_once_with("owned")
    github.poll_board.assert_awaited_once()
    assert session["board_paused"] is True
    assert session["agent_dispatch"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["monitoring_pr", "monitoring_performer", "dispatching"])
async def test_manual_block_pauses_work_without_synthesizing_questions(phase):
    daemon, session, service = daemon_with_worker("BLOCKED")
    session["phase"] = phase
    session["pr_artefacts"] = {"pr_node_id": "PR_existing", "pr_url": "https://github.com/example/repo/pull/1"}
    if phase == "monitoring_pr":
        session["current_card"]["status"] = "IN_REVIEW"
        session["agent_dispatch"] = {}
    await daemon._reconcile_board_pauses()
    assert session["board_paused"] is True
    assert session["board_pause_column"] == "BLOCKED"
    assert session["phase"] == "blocked"
    assert session["agent_dispatch"] == {}
    assert session["pr_artefacts"]["pr_node_id"] == "PR_existing"
    assert session["relay_feedback"] == [{"message": "keep instruction"}]
    await daemon._reconcile_board_pauses()
    assert session["board_paused"] is True
    if phase != "monitoring_pr":
        service.stop_session_confirmed.assert_awaited_once_with("owned")


@pytest.mark.asyncio
async def test_manual_block_pr_monitoring_resumes_same_pr_after_restart():
    daemon, session, _ = daemon_with_worker("BLOCKED")
    session.update(phase="monitoring_pr", agent_dispatch={})
    session["current_card"].update(status="IN_REVIEW", pr_node_id="PR_existing")
    await daemon._reconcile_board_pauses()
    persisted = _persist_one_session("card", session)
    restored = _restored_session_dict("card", persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked"), session["current_card"])
    daemon._state["active_sessions"] = {"card": restored}
    daemon._reconcile_session_phases({"card": restored}, {"BLOCKED": ["card"]}, None)
    await daemon._reconcile_board_pauses()
    assert restored["board_paused"] is True
    daemon._state["board_snapshot"] = {"TODO": ["card"]}
    await daemon._reconcile_board_pauses()
    assert restored["board_paused"] is False
    assert restored["phase"] == "monitoring_pr"
    assert restored["current_card"]["pr_node_id"] == "PR_existing"


@pytest.mark.asyncio
async def test_existing_clarification_block_keeps_recovery_path():
    daemon, session, service = daemon_with_worker("BLOCKED")
    session.update(phase="blocked", agent_dispatch={}, open_questions=["What should empty input return?"])
    session["current_card"]["status"] = "BLOCKED"
    await daemon._reconcile_board_pauses()
    assert not session.get("board_paused")
    assert session["open_questions"] == ["What should empty input return?"]
    service.stop_session_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_block_unconfirmed_stop_keeps_capacity():
    from coordinare.services.pipeline_budget import select_pipelines

    daemon, session, _ = daemon_with_worker("BLOCKED", stopped=False)
    await daemon._reconcile_board_pauses()
    assert session["board_paused"] is True
    assert session["agent_dispatch"]["session_id"] == "owned"
    assert select_pipelines({"card": session, "sibling": {"current_card": {"id": "sibling"}, "phase": "dispatching"}}, 1, {"sibling"}) == {"card"}


@pytest.mark.asyncio
async def test_startup_captures_new_manual_block_before_phase_inference():
    daemon, session, _ = daemon_with_worker("BLOCKED")
    session.update(phase="monitoring_pr", agent_dispatch={})
    session["current_card"]["status"] = "IN_REVIEW"
    daemon._reconcile_session_phases({"card": session}, {"BLOCKED": ["card"]}, None)
    assert session["board_paused"] is True
    assert session["board_pause_resume_phase"] == "monitoring_pr"
    await daemon._reconcile_board_pauses()
    assert session["phase"] == "blocked"
    daemon._state["board_snapshot"] = {"IN_REVIEW": ["card"]}
    await daemon._reconcile_board_pauses()
    assert session["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_confirmed_manual_pr_block_releases_slot_for_sibling():
    from coordinare.services.pipeline_budget import select_pipelines

    daemon, session, _ = daemon_with_worker("BLOCKED")
    session.update(phase="monitoring_pr", agent_dispatch={})
    await daemon._reconcile_board_pauses()
    sibling = {"current_card": {"id": "sibling"}, "phase": "dispatching"}
    assert select_pipelines({"card": session, "sibling": sibling}, 1, {"sibling"}) == {"sibling"}
    assert daemon._state["active_sessions"]["card"] is session

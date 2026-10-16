"""Shutdown refusal at the shared service and recovery of a mid-dispatch stop."""
from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare import __main__ as app_main
from coordinare import daemon as daemon_module
from coordinare.config import ProjectConfiguration
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.nodes.dispatch_performer import _execute_dispatch, dispatch_performer
from coordinare.models.env_cache import BootstrapJobPayload
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import http_performer_service
from coordinare.services.bootstrap_history import BootstrapHistory
from coordinare.services.documenting_side import run_cycle, should_dispatch
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.session import session_to_state, state_to_session
from tests.unit.graph.nodes.test_dispatch_performer import _base_state, _Service
from tests.unit.services.test_documenting_side import _resolve, _session
from tests.unit.test_173_daemon_intake import _state as _intake_state
from tests.unit.test_wiki_init_dispatch import _ec, _github, _workspace_managers


def _service(daemon, *, mode="ephemeral"):
    runtime = MagicMock()
    runtime.start_ephemeral = AsyncMock(side_effect=AssertionError("must not start while stopping"))
    client = MagicMock(post_job=AsyncMock())
    service = HTTPPerformerService(
        PerformerEndpointConfig(
            id="worker", mode=mode, roles=["documenting"],
            image="performer:test",
            endpoint="http://performer:8080" if mode == "persistent" else None,
        ),
        runtime=runtime, client=client,
    )
    service.bind_stop_event(daemon.state["stop_event"])
    return service, runtime, client


def _sigterm(daemon, monkeypatch):
    callbacks = {}
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler",
                        lambda sig, callback: callbacks.update({sig: callback}))
    daemon._install_signal_handlers()
    callbacks[signal.SIGTERM]()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ephemeral", "persistent"])
async def test_shared_dispatch_refuses_with_typed_stopping_outcome(monkeypatch, mode):
    daemon = CoordinareDaemon(None)
    service, runtime, client = _service(daemon, mode=mode)
    _sigterm(daemon, monkeypatch)
    with pytest.raises(http_performer_service.DispatchStoppingError, match="daemon is stopping"):
        await service.dispatch_card({"id": "c1", "role": "documenting"})
    runtime.start_ephemeral.assert_not_awaited()
    client.post_job.assert_not_awaited()
    assert service._active_jobs == {}


@pytest.mark.asyncio
async def test_dispatch_waiting_for_service_lock_rechecks_stop(monkeypatch):
    daemon = CoordinareDaemon(None)
    service, runtime, client = _service(daemon)
    async with service._dispatch_lock:
        pending = asyncio.create_task(service.dispatch_card({}))
        await asyncio.sleep(0)
        assert not pending.done()
        _sigterm(daemon, monkeypatch)
    with pytest.raises(http_performer_service.DispatchStoppingError):
        await pending
    runtime.start_ephemeral.assert_not_awaited()
    client.post_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["advocate", "curator"])
async def test_intake_shutdown_refusal_preserves_retry_budget(monkeypatch, role):
    from coordinare.config import CuratorConfig

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    daemon = CoordinareDaemon(None)
    service, runtime, client = _service(daemon)
    daemon.state.update(_intake_state(service, curator=CuratorConfig(enabled=True, github_repo="r")))
    ec = daemon.state["env_cache"]["s"]
    before = ec.model_dump()
    _sigterm(daemon, monkeypatch)
    await daemon._maybe_dispatch_intake(role, "s", SimpleNamespace(project_id=""))
    assert ec.model_dump() == before
    assert not daemon._intake_poll_tasks
    runtime.start_ephemeral.assert_not_awaited()
    client.post_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_wiki_init_shutdown_refusal_leaves_init_retryable(monkeypatch):
    daemon = CoordinareDaemon(None)
    service, runtime, client = _service(daemon)
    ec = _ec()
    daemon.state.update(env_cache={"sym": ec}, performer_services={"documenting": service},
                        symphony_workspace_managers=_workspace_managers(), config=None)
    before = ec.model_dump()
    warning = MagicMock()
    monkeypatch.setattr(daemon_module.logger, "warning", warning)
    _sigterm(daemon, monkeypatch)
    await daemon._execute_wiki_init_dispatch("sym", _github())
    assert ec.model_dump() == before
    assert not daemon._wiki_init_poll_tasks
    runtime.start_ephemeral.assert_not_awaited()
    client.post_job.assert_not_awaited()
    warning.assert_not_called()


@pytest.mark.asyncio
async def test_bootstrap_shutdown_refusal_preserves_budget_and_closes_history(monkeypatch):
    daemon = CoordinareDaemon(None)
    service, runtime, client = _service(daemon)
    ec = _ec()
    ec.bootstrap_in_flight = True  # env-cache sets this before calling the daemon
    ec.bootstrap_attempts = 2
    history = BootstrapHistory()
    daemon.state.update(env_cache={"sym": ec}, bootstrap_history=history, config=None)
    monkeypatch.setattr(daemon, "_resolve_bootstrap_volumes", lambda *_args: (None, {}))
    env_cache_service = MagicMock()
    payload = BootstrapJobPayload(symphony_name="sym", symphony_org="o", symphony_repo="r",
                                  cache_mount_path="/devenv/sym")
    _sigterm(daemon, monkeypatch)
    await daemon._execute_bootstrap_dispatch("worker", payload, "sym", {"worker": service}, env_cache_service)
    assert not ec.bootstrap_in_flight
    assert ec.bootstrap_attempts == 2 and not ec.bootstrap_exhausted
    assert ec.last_bootstrap_succeeded is None and ec.last_bootstrap_error is None
    env_cache_service.on_bootstrap_complete.assert_not_called()
    assert not daemon._bootstrap_poll_tasks
    assert history.open_attempt("sym") is None
    assert all(record.outcome != "failed" for record in history.recent("sym", 10))
    runtime.start_ephemeral.assert_not_awaited()
    client.post_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_documenting_shutdown_refusal_does_not_consume_blueprint(monkeypatch):
    daemon = CoordinareDaemon(None)
    service, runtime, client = _service(daemon)
    session = _session()
    polls = []
    _sigterm(daemon, monkeypatch)
    assert await run_cycle({"c1": session}, svc=service, resolve=_resolve, spawn=polls.append) == 0
    assert session["documenting_side"] is None
    assert should_dispatch(session)[0]
    assert polls == []
    runtime.start_ephemeral.assert_not_awaited()
    client.post_job.assert_not_awaited()


def test_service_builder_shares_stop_event_with_stage_and_bootstrap_services():
    stop_event = asyncio.Event()
    config = ProjectConfiguration(
        github_org="o", github_token="test-token", human_reviewers=["operator"],
        performer_endpoints=[
            PerformerEndpointConfig(id="doc", mode="ephemeral", roles=["documenting"], image="performer:test"),
            PerformerEndpointConfig(id="bootstrap", mode="ephemeral", roles=["env_bootstrap"], image="performer:test"),
        ],
    )
    stages, bootstrap = app_main._build_http_performer_services(config, stop_event=stop_event)
    assert stages["documenting"][0]._stop_event is stop_event
    assert bootstrap["bootstrap"]._stop_event is stop_event


@pytest.mark.asyncio
async def test_daemon_initializer_passes_shared_stop_event_to_service_bootstrap(monkeypatch):
    daemon = CoordinareDaemon(None)
    daemon.state["activity_log"] = None
    monkeypatch.setattr(daemon, "start", AsyncMock())
    monkeypatch.setattr(app_main, "load_unexpanded_config", MagicMock(return_value={}))
    monkeypatch.setattr(app_main, "run_preflight", AsyncMock(return_value=MagicMock(ok=True, results=[])))
    bootstrap = AsyncMock(return_value={"github_service": MagicMock(field_cache={})})
    monkeypatch.setattr(app_main, "_bootstrap_services", bootstrap)
    config = ProjectConfiguration(github_org="o", github_token="test-token", human_reviewers=["operator"])
    await app_main._initialize_daemon(config, None, None, daemon, {})
    assert bootstrap.call_args.kwargs["stop_event"] is daemon.state["stop_event"]


@pytest.mark.asyncio
async def test_graph_handles_shared_service_refusal_without_consuming_feedback_or_slot():
    service = SimpleNamespace(dispatch_card=AsyncMock(side_effect=http_performer_service.DispatchStoppingError()))
    board = MagicMock(move_card=AsyncMock())
    release = MagicMock()
    state = _base_state(phase="dispatching", relay_feedback=[{"text": "retry"}])
    context = {"service": service, "board_provider": board, "card_id": "ITEM_1", "card_context": {}}
    result = await _execute_dispatch(state, context, None, release)
    assert result is state
    assert result["phase"] == "dispatching" and not result.get("agent_dispatch")
    assert result["relay_feedback"] == [{"text": "retry"}]
    release.assert_called_once_with()
    board.move_card.assert_awaited_once_with("ITEM_1", "IN_PROGRESS")


@pytest.mark.asyncio
async def test_shutdown_after_board_move_is_recovered_by_startup_reconciliation(monkeypatch):
    """094 board reconcile plus container reconcile recovers an actual refused dispatch."""
    daemon = CoordinareDaemon(None)
    service = _Service()
    daemon.state.update(_base_state(phase="dispatching", performer_stage="implementing",
                                    performer_services={"implementing": service}))
    board = daemon.state["github_service"]
    original_move = board.move_card

    async def move_then_stop(card_id, column):
        await original_move(card_id, column)
        daemon.stop()

    monkeypatch.setattr(board, "move_card", move_then_stop)
    result = await dispatch_performer(dict(daemon.state))
    assert board.move_calls == [("ITEM_1", "IN_PROGRESS")]
    assert result["phase"] == "dispatching" and not result.get("agent_dispatch")
    assert service.dispatched == []
    daemon.state.update(result)
    daemon.state["active_card_id"] = "ITEM_1"
    daemon.state["active_sessions"] = {"ITEM_1": state_to_session(daemon.state)}
    snapshot = daemon._build_snapshot()
    assert snapshot.active_sessions["ITEM_1"].phase == "dispatching"

    restarted = CoordinareDaemon(None)
    restarted.state.update(_base_state(performer_services={"implementing": service}))
    restarted._restore_from_snapshot(snapshot)
    github = MagicMock(project_id=1, poll_board=AsyncMock(return_value={"snapshot": {"IN_PROGRESS": ["ITEM_1"]}}))
    restarted.state["github_service"] = github
    await restarted._reconcile_with_board(snapshot)
    await restarted._startup_reconciliation_pass()
    session = restarted.state["active_sessions"]["ITEM_1"]
    assert session["phase"] == "dispatching"
    assert session["reconciled_dispatch_pending"] is True
    assert session["performer_stage"] == "implementing"
    assert not session.get("agent_dispatch")

    session_to_state(session, restarted.state)
    # Use the normal board stub for a real successful redispatch.
    monkeypatch.setattr(board, "move_card", original_move)
    restarted.state["github_service"] = board
    retried = await dispatch_performer(restarted.state)
    assert len(service.dispatched) == 1
    assert retried["phase"] == "monitoring_performer"
    assert retried["agent_dispatch"]["session_id"] == "sess-1"

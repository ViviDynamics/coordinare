from __future__ import annotations

import asyncio
import signal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from coordinare import __main__ as app_main
from coordinare.config import ProjectConfiguration
from coordinare.http_server import HTTP_DRAIN_SECONDS, ManagedServer
from tests.unit.test_299_dashboard_deploy import _daemon

WAIT_SECONDS = 2
TOKEN = "x" * 32


@pytest.mark.asyncio
async def test_listeners_precede_preflight_and_sigterm_drains_before_stopping_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProjectConfiguration(github_org="example", human_reviewers=["operator"], github_token=TOKEN)
    daemon = _daemon()
    monkeypatch.setattr(app_main, "CoordinareDaemon", MagicMock(return_value=daemon))
    monkeypatch.setattr(app_main, "StateStore", MagicMock())
    monkeypatch.setattr(app_main, "check_port_available", MagicMock())
    monkeypatch.setattr(app_main, "load_unexpanded_config", MagicMock(return_value={}))
    bootstrap = AsyncMock()
    monkeypatch.setattr(app_main, "_bootstrap_services", bootstrap)
    preflight_entered = asyncio.Event()
    preflight_block = asyncio.Event()
    preflight_cancelled = asyncio.Event()
    request_finished = asyncio.Event()
    response_completed = asyncio.Event()
    request_entered = asyncio.Event()
    servers: list[Any] = []
    callbacks: dict[signal.Signals, Any] = {}

    class FakeServer:
        def __init__(self, server_config: Any) -> None:
            self.config = server_config
            self.started = False
            self.exit_event = asyncio.Event()
            self.draining = asyncio.Event()
            servers.append(self)

        @property
        def should_exit(self) -> bool:
            return self.exit_event.is_set()

        @should_exit.setter
        def should_exit(self, value: bool) -> None:
            if value:
                self.exit_event.set()

        async def serve(self) -> None:
            self.started = True
            await self.exit_event.wait()
            self.draining.set()
            if self.config.port == config.dashboard_port:
                await response_completed.wait()

    async def preflight(*args: Any) -> None:
        assert len(servers) == 2 and all(server.started for server in servers)
        preflight_entered.set()
        try:
            await preflight_block.wait()
        finally:
            preflight_cancelled.set()

    monkeypatch.setattr(app_main, "ManagedServer", FakeServer)
    monkeypatch.setattr(app_main, "run_preflight", preflight)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: callbacks.update({sig: callback}))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)
    runtime = asyncio.create_task(app_main._run_with_events(config, None, None, "legacy", MagicMock()))
    try:
        await asyncio.wait_for(preflight_entered.wait(), WAIT_SECONDS)
        dashboard = next(server for server in servers if server.config.port == config.dashboard_port)
        assert dashboard.config.timeout_graceful_shutdown == HTTP_DRAIN_SECONDS
        @dashboard.config.app.get("/drain")
        async def in_flight_request() -> dict[str, str]:
            request_entered.set()
            await request_finished.wait()
            return {"status": "finished"}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard.config.app), base_url="http://127.0.0.1:8090",
        ) as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/api/force-poll")).status_code == 503
            request = asyncio.create_task(client.get("/drain"))
            await asyncio.wait_for(request_entered.wait(), WAIT_SECONDS)
            callbacks[signal.SIGTERM]()
            await asyncio.wait_for(dashboard.draining.wait(), WAIT_SECONDS)
            daemon.stop.assert_called_once()
            await asyncio.wait_for(preflight_cancelled.wait(), WAIT_SECONDS)
            request_finished.set()
            assert (await asyncio.wait_for(request, WAIT_SECONDS)).json() == {"status": "finished"}
            response_completed.set()
        await asyncio.wait_for(runtime, WAIT_SECONDS)
        daemon.stop.assert_called_once()
        bootstrap.assert_not_awaited()
    finally:
        request_finished.set()
        response_completed.set()
        if not runtime.done():
            runtime.cancel()
            await asyncio.gather(runtime, return_exceptions=True)


def test_http_servers_leave_signal_ownership_to_process(monkeypatch: pytest.MonkeyPatch) -> None:
    signal_handler = MagicMock()
    monkeypatch.setattr(signal, "signal", signal_handler)
    server = ManagedServer(MagicMock())
    with server.capture_signals():
        signal_handler.assert_not_called()


@pytest.mark.asyncio
async def test_hung_initializer_exits_and_liveness_fails(monkeypatch) -> None:
    config = ProjectConfiguration(github_org="example", human_reviewers=["operator"],
                                  github_token=TOKEN, startup_deadline_seconds=0.03)
    daemon = _daemon()
    servers = []
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False
            servers.append(self)

        async def serve(self):
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.001)

    async def hung(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(app_main, "CoordinareDaemon", MagicMock(return_value=daemon))
    monkeypatch.setattr(app_main, "StateStore", MagicMock())
    monkeypatch.setattr(app_main, "check_port_available", MagicMock())
    monkeypatch.setattr(app_main, "ManagedServer", FakeServer)
    monkeypatch.setattr(app_main, "_initialize_daemon", hung)
    error_log = MagicMock()
    monkeypatch.setattr(app_main.logger, "error", error_log)
    task = asyncio.create_task(app_main._run_with_events(config, None, None, "legacy", MagicMock()))
    await asyncio.wait_for(entered.wait(), WAIT_SECONDS)
    with pytest.raises(app_main.RuntimeExecutionError, match="startup"):
        await asyncio.wait_for(task, WAIT_SECONDS)
    assert cancelled.is_set()
    assert any(call.args[0] == "startup.deadline_exceeded" for call in error_log.call_args_list)
    health = next(server.config.app for server in servers if server.config.port == config.health_check_port)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=health), base_url="http://health") as client:
        response = await client.get("/live")
        assert response.status_code == 503
        assert response.json()["status"] == "startup_timeout"
        daemon.state["startup_complete"] = True
        assert (await client.get("/live")).status_code == 200


@pytest.mark.asyncio
async def test_signal_prevents_next_poll_cycle(monkeypatch) -> None:
    from coordinare.daemon import CoordinareDaemon

    daemon = CoordinareDaemon(None, install_signal_handlers=False)
    monkeypatch.setattr(daemon, "_run_startup_sequence", AsyncMock(return_value=("idle", None)))
    monkeypatch.setattr(daemon, "_start_transcript_retention", MagicMock(return_value=None))
    monkeypatch.setattr(daemon, "_start_run_log_retention", MagicMock(return_value=None))
    cycle = AsyncMock()
    entered = asyncio.Event()

    async def orchestration(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(daemon, "_begin_cycle", cycle)
    monkeypatch.setattr(daemon, "_run_orchestration", orchestration)
    monkeypatch.setattr(daemon, "_shutdown_flush", AsyncMock())
    task = asyncio.create_task(daemon.start())
    await asyncio.wait_for(entered.wait(), WAIT_SECONDS)
    daemon.stop()
    await asyncio.wait_for(task, WAIT_SECONDS)
    cycle.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_wait_is_bounded_for_stuck_task() -> None:
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def stubborn():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    try:
        await asyncio.wait_for(app_main._finish_tasks({task}, within=0.01), WAIT_SECONDS)
        await asyncio.wait_for(cancelled.wait(), WAIT_SECONDS)
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_startup_watchdog_retires_after_startup() -> None:
    from time import monotonic

    daemon = _daemon()
    daemon.state["startup_complete"] = True
    await app_main._watch_startup(daemon, monotonic() - 1)


@pytest.mark.asyncio
async def test_snapshot_shutdown_has_its_own_deadline(monkeypatch) -> None:
    from coordinare import daemon as daemon_module

    daemon = daemon_module.CoordinareDaemon(None, state_store=MagicMock())
    cancelled = asyncio.Event()

    async def hung_save(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    daemon.state_store.save = AsyncMock(side_effect=hung_save)
    monkeypatch.setattr(daemon_module, "SHUTDOWN_SNAPSHOT_SECONDS", 0.01, raising=False)
    await asyncio.wait_for(daemon._shutdown_flush(object()), 0.1)
    assert cancelled.is_set()


@pytest.mark.parametrize("value", [0, -1, 3601])
def test_startup_deadline_rejects_invalid_values(value) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="startup_deadline_seconds"):
        ProjectConfiguration(github_org="example", human_reviewers=["operator"], github_token=TOKEN, startup_deadline_seconds=value)


def test_startup_deadline_defaults_to_ten_minutes() -> None:
    assert ProjectConfiguration(github_org="example", human_reviewers=["operator"], github_token=TOKEN).startup_deadline_seconds == 600


@pytest.mark.asyncio
async def test_event_store_shutdown_is_bounded(monkeypatch) -> None:
    config = ProjectConfiguration(github_org="example", human_reviewers=["operator"], github_token=TOKEN)
    cancelled = asyncio.Event()

    async def stuck_close():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    store = MagicMock(start=AsyncMock(), close=AsyncMock(side_effect=stuck_close))
    monkeypatch.setattr(app_main, "EventStore", MagicMock(return_value=store))
    monkeypatch.setattr(app_main, "_run_with_events", AsyncMock())
    monkeypatch.setattr(app_main, "load_unexpanded_config", MagicMock(return_value={}))
    monkeypatch.setattr(app_main, "EVENT_STORE_SHUTDOWN_SECONDS", 0.01, raising=False)
    await asyncio.wait_for(app_main._run(config), 0.1)
    await asyncio.wait_for(cancelled.wait(), WAIT_SECONDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("signal_at", ["before_dispatch", "health", "board_move"])
async def test_sigterm_mid_cycle_refuses_dispatch_from_copied_state(monkeypatch, signal_at) -> None:
    from coordinare import daemon as daemon_module
    from coordinare.graph.nodes.dispatch_performer import dispatch_performer
    from tests.unit.graph.nodes.test_dispatch_performer import _base_state, _Service

    daemon = daemon_module.CoordinareDaemon(None)
    service = _Service()
    slots = MagicMock()
    slots.acquire.return_value = service
    daemon.state.update(_base_state(
        phase="dispatching", performer_stage="implementing",
        performer_services={"implementing": service}, slot_manager=slots,
        relay_feedback=[{"kind": "test", "text": "retry after restart"}],
    ))
    callbacks = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: callbacks.update({sig: callback}))
    daemon._install_signal_handlers()
    copied_state = dict(daemon.state)
    log = MagicMock()
    monkeypatch.setattr(daemon_module.logger, "info", log)

    def terminate():
        callbacks[signal.SIGTERM]()
        callbacks[signal.SIGTERM]()

    if signal_at == "before_dispatch":
        terminate()
    elif signal_at == "health":
        async def health():
            terminate()
            return {"status": "idle"}
        monkeypatch.setattr(service, "check_health", health)
    else:
        async def move(*args):
            terminate()
        monkeypatch.setattr(copied_state["github_service"], "move_card", move)

    result = await dispatch_performer(copied_state)
    assert service.dispatched == []
    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert result["current_card"]["id"] == "ITEM_1"
    assert not result.get("agent_dispatch")
    assert result["relay_feedback"] == [{"kind": "test", "text": "retry after restart"}]
    if signal_at == "before_dispatch":
        slots.acquire.assert_not_called()
    else:
        slots.release.assert_called_once_with("implementing", "ITEM_1")
    assert daemon.state["stopping"] is True
    assert [call.args[0] for call in log.call_args_list].count("daemon.dispatch_stopped") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stuck_conductor", [False, True])
@pytest.mark.parametrize("stuck_http", [False, True])
async def test_sigterm_long_cycle_saves_final_snapshot_outside_runtime(monkeypatch, tmp_path, stuck_conductor, stuck_http) -> None:
    from coordinare import daemon as daemon_module
    from coordinare.daemon import CoordinareDaemon
    from coordinare.services.conductor_calls import ConductorCallLog
    from coordinare.state_store import StateStore

    config = ProjectConfiguration(github_org="example", human_reviewers=["operator"], github_token=TOKEN)
    store = StateStore(tmp_path / "state.json", MagicMock())
    daemon = CoordinareDaemon(None, state_store=store, install_signal_handlers=False)
    entered = asyncio.Event()
    first_cancel = asyncio.Event()
    cycle_finished = asyncio.Event()
    callbacks = {}
    save_tasks = []
    drain_tasks = []
    original_save = store.save

    async def drain(**kwargs):
        drain_tasks.append(asyncio.current_task().get_name())
        assert cycle_finished.is_set()
        if stuck_conductor:
            await asyncio.Event().wait()

    calls = ConductorCallLog(MagicMock())
    monkeypatch.setattr(calls, "drain", drain)
    daemon.state["conductor_calls"] = calls
    monkeypatch.setattr(daemon_module, "SHUTDOWN_DRAIN_SECONDS", 0.01)

    async def save(snapshot):
        save_tasks.append(asyncio.current_task().get_name())
        assert cycle_finished.is_set()
        await asyncio.sleep(0.02)
        await original_save(snapshot)

    async def startup():
        daemon.state["startup_complete"] = True
        return "idle", daemon._lifecycle_signature()

    async def long_cycle(*args):
        daemon.state["phase"] = "dispatching"
        daemon.state["performer_stage"] = "implementing"
        entered.set()
        try:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_cancel.set()
                await asyncio.Event().wait()
        finally:
            cycle_finished.set()

    class FakeServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def serve(self):
            self.started = True
            if stuck_http:
                await asyncio.Event().wait()
            while not self.should_exit:
                await asyncio.sleep(0.001)

    monkeypatch.setattr(app_main, "CoordinareDaemon", MagicMock(return_value=daemon))
    monkeypatch.setattr(app_main, "StateStore", MagicMock(return_value=store))
    monkeypatch.setattr(app_main, "check_port_available", MagicMock())
    monkeypatch.setattr(app_main, "ManagedServer", FakeServer)
    monkeypatch.setattr(app_main, "load_unexpanded_config", MagicMock(return_value={}))
    monkeypatch.setattr(app_main, "run_preflight", AsyncMock(return_value=MagicMock(ok=True, results=[])))
    monkeypatch.setattr(app_main, "_bootstrap_services", AsyncMock(return_value={
        "github_service": MagicMock(field_cache={}),
    }))
    monkeypatch.setattr(app_main, "RUNTIME_SHUTDOWN_SECONDS", 0.01)
    monkeypatch.setattr(app_main, "HTTP_SHUTDOWN_SECONDS", 0.01)
    monkeypatch.setattr(daemon, "_run_startup_sequence", startup)
    monkeypatch.setattr(daemon, "_start_transcript_retention", lambda: None)
    monkeypatch.setattr(daemon, "_start_run_log_retention", lambda: None)
    monkeypatch.setattr(daemon, "_run_orchestration", long_cycle)
    monkeypatch.setattr(store, "save", save)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: callbacks.update({sig: callback}))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)
    runtime = asyncio.create_task(app_main._run_with_events(config, None, None, "legacy", MagicMock()))
    try:
        await asyncio.wait_for(entered.wait(), WAIT_SECONDS)
        callbacks[signal.SIGTERM]()
        await asyncio.wait_for(first_cancel.wait(), WAIT_SECONDS)
        await asyncio.wait_for(runtime, WAIT_SECONDS)
        snapshot = await store.load()
        assert snapshot is not None, "SIGTERM dropped the final snapshot"
        assert snapshot.phase == "dispatching"
        assert snapshot.performer_stage == "implementing"
        assert save_tasks and "daemon_runtime" not in save_tasks
        assert drain_tasks and "daemon_runtime" not in drain_tasks
        assert daemon._main_task.done()
    finally:
        if daemon._main_task is not None and not daemon._main_task.done():
            daemon._main_task.cancel()
            await asyncio.gather(daemon._main_task, return_exceptions=True)
        if not runtime.done():
            runtime.cancel()
        await asyncio.gather(runtime, return_exceptions=True)



def _bound_dashboard_port() -> tuple[Any, int]:
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener, listener.getsockname()[1]


@pytest.mark.asyncio
async def test_the_daemon_preflight_accepts_its_own_bound_dashboard_port(monkeypatch) -> None:
    """299 regression: the dashboard binds before the runtime preflight, which must not see it as taken.

    Every launch crash-looped (exit 1, "dashboard port" FAIL) until the preflight recognised the
    listener this process had already started. Another process on the port is still a conflict.
    """
    from coordinare import launch_preflight

    monkeypatch.setattr(launch_preflight, "check_directories", lambda config: [])
    monkeypatch.setattr(launch_preflight, "run_checks", lambda config: launch_preflight.DoctorReport())
    monkeypatch.setattr(launch_preflight, "check_harnesses", AsyncMock(return_value=[]))
    monkeypatch.setattr(launch_preflight, "check_native_models", lambda config: [])
    monkeypatch.setattr(launch_preflight, "check_github", AsyncMock(return_value=[]))
    listener, port = _bound_dashboard_port()
    try:
        config = ProjectConfiguration(
            github_org="example", human_reviewers=["operator"], github_token=TOKEN,
            dashboard_host="127.0.0.1", dashboard_port=port,
        )
        doctor = await launch_preflight.run_preflight(config)
        assert "dashboard port" in [result.name for result in doctor.failed], "someone else's listener is a conflict"
        launch_preflight.mark_serving("127.0.0.1", port)
        try:
            daemon = await launch_preflight.run_preflight(config)
            assert "dashboard port" not in [result.name for result in daemon.failed]
        finally:
            launch_preflight.stop_serving("127.0.0.1", port)
        again = await launch_preflight.run_preflight(config)
        assert "dashboard port" in [result.name for result in again.failed], "stop_serving clears the mark"
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_the_daemon_marks_its_dashboard_before_the_runtime_preflight(monkeypatch) -> None:
    from coordinare import launch_preflight

    seen: list[bool] = []

    async def preflight(config, *args, **kwargs):
        seen.append((config.dashboard_host, config.dashboard_port) in launch_preflight._SERVING)
        return MagicMock(ok=False, results=[], failed=[], render=lambda: "")

    monkeypatch.setattr(app_main, "run_preflight", preflight)
    monkeypatch.setattr(app_main, "CoordinareDaemon", MagicMock(return_value=_daemon()))
    monkeypatch.setattr(app_main, "StateStore", MagicMock())
    monkeypatch.setattr(app_main, "check_port_available", MagicMock())
    monkeypatch.setattr(app_main, "load_unexpanded_config", MagicMock(return_value={}))
    monkeypatch.setattr(app_main, "_bootstrap_services", AsyncMock())
    config = ProjectConfiguration(
        github_org="example", human_reviewers=["operator"], github_token=TOKEN, dashboard_port=0,
    )
    with pytest.raises((app_main.RuntimeExecutionError, SystemExit)):
        await asyncio.wait_for(app_main._run(config), timeout=20)
    assert seen == [True]
    assert (config.dashboard_host, config.dashboard_port) not in launch_preflight._SERVING

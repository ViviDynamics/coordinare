"""Fixtures for E2E browser tests: live uvicorn server backed by a real DashboardStore."""
from __future__ import annotations

import asyncio
import queue as _queue
import socket
import threading
import time
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest
import uvicorn

from coordinare.dashboard import DashboardStore, create_dashboard_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _mock_daemon(phase: str = "idle") -> Any:
    daemon = MagicMock()
    daemon.state = {"phase": phase, "error_count": 0}
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    daemon._webhook_trigger = asyncio.Event()
    return daemon


def _mock_metrics() -> Any:
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": "2026-03-02T09:30:00+00:00",
    }
    return metrics


def _mock_health() -> Any:
    health = MagicMock()
    probe = MagicMock()
    probe.subsystem_name = "github"
    probe.status.value = "healthy"
    probe.is_required = True
    probe.checked_at.isoformat.return_value = "2026-03-02T10:00:00+00:00"
    probe.details = None
    health.snapshot.return_value.probes = [probe]
    return health


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def store() -> DashboardStore:
    """Shared DashboardStore for the entire test module."""
    return DashboardStore()


SERVER_START_SECONDS = 5.0
SERVER_JOIN_SECONDS = 5.0
SERVER_POLL_SECONDS = 0.05
LOOP_CAPTURE_SECONDS = 10.0


@pytest.fixture(scope="module")
def live_server_lifecycle() -> dict[str, Any]:
    """Control the live server on its original port for rollout tests."""
    return {}


@pytest.fixture(scope="module")
def live_server_url(
    store: DashboardStore, live_server_lifecycle: dict[str, Any],
) -> Generator[str, None, None]:
    """Start the dashboard, retaining the same port when a test restarts it."""
    port = _free_port()
    original_broadcast = store.broadcaster.broadcast
    active: dict[str, Any] = {}

    def start(*, starting: bool = False) -> None:
        daemon = _mock_daemon()
        daemon.state["startup_complete"] = not starting
        daemon.running = not starting
        app = create_dashboard_app(store, daemon, _mock_metrics(), _mock_health())
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error",
            timeout_graceful_shutdown=SERVER_JOIN_SECONDS,
        )
        server = uvicorn.Server(config)
        loop_queue: _queue.SimpleQueue[asyncio.AbstractEventLoop] = _queue.SimpleQueue()

        async def run() -> None:
            loop_queue.put(asyncio.get_running_loop())
            await server.serve()

        thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
        active.update(server=server, thread=thread, daemon=daemon)
        thread.start()
        deadline = time.monotonic() + SERVER_START_SECONDS
        while time.monotonic() < deadline:
            if server.started:
                break
            time.sleep(SERVER_POLL_SECONDS)
        else:
            server.should_exit = True
            thread.join(timeout=SERVER_JOIN_SECONDS)
            pytest.fail("Live server did not start within the startup budget")
        server_loop = loop_queue.get(timeout=LOOP_CAPTURE_SECONDS)
        active["loop"] = server_loop
        # Schedule Queue operations on the owning event loop, including after restart.
        store.broadcaster.broadcast = lambda payload: server_loop.call_soon_threadsafe(original_broadcast, payload)

    def stop() -> None:
        if not active or not active["thread"].is_alive():
            return
        active["loop"].call_soon_threadsafe(store.shutdown)
        active["server"].should_exit = True
        active["thread"].join(timeout=SERVER_JOIN_SECONDS)
        assert not active["thread"].is_alive(), "Live server did not drain"

    def ready() -> None:
        def mark_ready() -> None:
            active["daemon"].state["startup_complete"] = True
            active["daemon"].running = True
        active["loop"].call_soon_threadsafe(mark_ready)

    live_server_lifecycle.update(start=start, stop=stop, ready=ready)
    start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop()
        store.broadcaster.broadcast = original_broadcast

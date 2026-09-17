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


@pytest.fixture(scope="module")
def live_server_url(store: DashboardStore) -> Generator[str, None, None]:
    """Start a real uvicorn server in a background thread and yield its base URL.

    Uses a module scope so the server starts once per test module, keeping
    browser test runtime low while still providing a real HTTP/SSE endpoint.
    """
    port = _free_port()
    app = create_dashboard_app(store, _mock_daemon(), _mock_metrics(), _mock_health())
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    # Capture the server's event loop so we can schedule broadcasts onto it
    # from the pytest (non-async) thread.  asyncio.Queue is not thread-safe;
    # calling put_nowait() from a foreign thread can corrupt waiters.
    _loop_q: _queue.SimpleQueue[asyncio.AbstractEventLoop] = _queue.SimpleQueue()

    async def _run_and_capture() -> None:
        _loop_q.put(asyncio.get_running_loop())
        await server.serve()

    thread = threading.Thread(target=lambda: asyncio.run(_run_and_capture()), daemon=True)
    thread.start()

    # Poll until the server is accepting connections (max 5 s)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("Live server did not start within 5 seconds")

    # Wrap store.broadcaster.broadcast to be thread-safe: schedule onto the
    # server's event loop rather than calling put_nowait() directly.
    server_loop = _loop_q.get(timeout=10.0)
    _orig_broadcast = store.broadcaster.broadcast
    store.broadcaster.broadcast = lambda p: server_loop.call_soon_threadsafe(_orig_broadcast, p)  # type: ignore[method-assign]

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)

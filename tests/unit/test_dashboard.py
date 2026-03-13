"""Tests for src/coordinare/dashboard.py (spec 010-web-dashboard)."""
from __future__ import annotations

import asyncio
import socket
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from coordinare.dashboard import (
    _DASHBOARD_HTML,
    DashboardStore,
    SSEBroadcaster,
    check_port_available,
    create_dashboard_app,
    format_phase_label,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_mock_daemon(
    phase: str = "idle",
    snapshot: Any = None,
    error_count: int = 0,
) -> MagicMock:
    daemon = MagicMock()
    daemon.state = {"phase": phase, "error_count": error_count}
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = snapshot
    return daemon


def _make_mock_metrics(cycles: int = 0, started_at: str | None = None) -> MagicMock:
    metrics = MagicMock()
    # Simulate prometheus counter _value.get()
    metrics.cycles_completed_total._value.get.return_value = cycles
    # Simulate build_info labels()._value.get()
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": started_at or "2026-03-02T09:30:00+00:00"
    }
    return metrics


def _make_mock_health(probes: list[dict] | None = None) -> MagicMock:
    health = MagicMock()
    if probes is None:
        probes = [
            {
                "name": "github",
                "status": "healthy",
                "required": True,
                "checked_at": "2026-03-02T10:00:00+00:00",
                "details": None,
            }
        ]

    mock_probe_objects = []
    for p in probes:
        probe = MagicMock()
        probe.subsystem_name = p["name"]
        probe.status.value = p["status"]
        probe.is_required = p["required"]
        probe.checked_at.isoformat.return_value = p["checked_at"]
        probe.details = p["details"]
        mock_probe_objects.append(probe)

    health.snapshot.return_value.probes = mock_probe_objects
    return health


def _make_app(
    store: DashboardStore | None = None,
    daemon: Any = None,
    metrics: Any = None,
    health: Any = None,
) -> TestClient:
    store = store or DashboardStore()
    daemon = daemon or _make_mock_daemon()
    metrics = metrics or _make_mock_metrics()
    health = health or _make_mock_health()
    app = create_dashboard_app(store, daemon, metrics, health)
    return TestClient(app)


# ---------------------------------------------------------------------------
# T036 — HTML size budget
# ---------------------------------------------------------------------------


def test_dashboard_html_under_28kb() -> None:
    """T036: _DASHBOARD_HTML must not exceed the 28 KB size budget (raised to accommodate Performers card)."""
    size = len(_DASHBOARD_HTML.encode())
    assert size < 28 * 1024, f"_DASHBOARD_HTML is {size} bytes (limit: {28 * 1024})"


# ---------------------------------------------------------------------------
# T014 — format_phase_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("monitoring_agent", "Monitoring Agent"),
        ("relay_feedback", "Relay Feedback"),
        ("idle", "Idle"),
        ("dispatching", "Dispatching"),
        ("blocked", "Blocked"),
        ("merging", "Merging"),
        ("unknown_future_phase", "Unknown Future Phase"),
    ],
)
def test_format_phase_label(phase: str, expected: str) -> None:
    assert format_phase_label(phase) == expected


# ---------------------------------------------------------------------------
# T015 — GET / and initial SSE event
# ---------------------------------------------------------------------------


def test_get_root_returns_200_html() -> None:
    client = _make_app()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Coordinare Dashboard" in resp.text
    assert "EventSource" in resp.text


def test_sse_initial_event_is_state_update() -> None:
    """sse_stream() must yield a state_update event with the correct shape as its first chunk."""
    import json as _json

    async def _collect() -> str:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        metrics = _make_mock_metrics()
        health = _make_mock_health()
        gen = store.sse_stream(daemon, metrics, health)
        try:
            first_chunk = await gen.__anext__()
        finally:
            await gen.aclose()
        return first_chunk

    first_chunk = asyncio.run(_collect())
    assert first_chunk.startswith("event: state_update\n"), repr(first_chunk)
    data_line = next(line for line in first_chunk.splitlines() if line.startswith("data:"))
    payload = _json.loads(data_line[len("data:"):].strip())
    for key in ("phase", "phase_label", "subsystems", "cycles_completed", "cycle_history"):
        assert key in payload, f"Missing key {key!r} in SSE payload"


# ---------------------------------------------------------------------------
# T016 — check_port_available exits on conflict
# ---------------------------------------------------------------------------


def test_check_port_available_exits_on_conflict() -> None:
    """Should call sys.exit(1) when the port is already bound."""
    # Bind a port so it appears in-use
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        # Port is now bound — check_port_available should fail
        with pytest.raises(SystemExit) as exc_info:
            check_port_available("127.0.0.1", port)
    assert exc_info.value.code == 1


def test_check_port_available_succeeds_on_free_port() -> None:
    """Should not raise when port is free."""
    # Find a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # Port released — should be free now
    check_port_available("127.0.0.1", port)  # no exception


# ---------------------------------------------------------------------------
# T017 — middleware logs requests
# ---------------------------------------------------------------------------


def test_middleware_logs_http_request(caplog: pytest.LogCaptureFixture) -> None:
    """HTTP middleware must emit an http_request structlog event."""
    import structlog.testing

    client = _make_app()
    with structlog.testing.capture_logs() as cap_logs:
        client.get("/")

    http_events = [e for e in cap_logs if e.get("event") == "http_request"]
    assert http_events, f"No http_request log emitted; got: {[e.get('event') for e in cap_logs]}"
    ev = http_events[0]
    assert ev["method"] == "GET"
    assert ev["path"] == "/"
    assert isinstance(ev["status_code"], int)
    assert isinstance(ev["response_time_ms"], float)


# ---------------------------------------------------------------------------
# T021/T022 — build_snapshot subsystem and metrics fields
# ---------------------------------------------------------------------------


def test_build_snapshot_contains_subsystem_fields() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics(cycles=5)
    health = _make_mock_health([
        {"name": "github", "status": "healthy", "required": True,
         "checked_at": "2026-03-02T10:00:00+00:00", "details": None},
        {"name": "agent_ssh", "status": "degraded", "required": True,
         "checked_at": "2026-03-02T10:00:00+00:00", "details": "circuit open"},
    ])
    snap = store.build_snapshot(daemon, metrics, health)
    assert len(snap["subsystems"]) == 2
    names = {s["name"] for s in snap["subsystems"]}
    assert names == {"github", "agent_ssh"}
    degraded = next(s for s in snap["subsystems"] if s["name"] == "agent_ssh")
    assert degraded["status"] == "degraded"
    assert degraded["details"] == "circuit open"


def test_build_snapshot_metrics_fields() -> None:
    store = DashboardStore()
    store.last_cycle_duration = 1.23
    daemon = _make_mock_daemon(error_count=2)
    metrics = _make_mock_metrics(cycles=17, started_at="2026-03-02T09:30:00+00:00")
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["cycles_completed"] == 17
    assert snap["last_cycle_duration_seconds"] == 1.23
    assert snap["consecutive_error_count"] == 2
    assert snap["daemon_start_time"] == "2026-03-02T09:30:00+00:00"


def test_build_snapshot_idle_nulls() -> None:
    """When no snapshot in state_store, card fields must be null."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state_store.last_snapshot = None
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["active_card_title"] is None
    assert snap["active_card_column"] is None
    assert snap["pr_url"] is None
    assert snap["agent_session_id"] is None
    assert snap["open_questions"] == []


def test_build_snapshot_active_card() -> None:
    store = DashboardStore()
    mock_snapshot = MagicMock()
    mock_snapshot.phase = "monitoring_agent"
    mock_snapshot.active_card_title = "Fix timeout"
    mock_snapshot.active_card_column = "In Progress"
    mock_snapshot.pr_url = "https://github.com/org/repo/pull/42"
    mock_snapshot.agent_session_id = "sess-abc"
    mock_snapshot.open_questions = []
    daemon = _make_mock_daemon(snapshot=mock_snapshot)
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["active_card_title"] == "Fix timeout"
    assert snap["pr_url"] == "https://github.com/org/repo/pull/42"
    assert snap["phase_label"] == "Monitoring Agent"


# ---------------------------------------------------------------------------
# T028 — record_cycle and history
# ---------------------------------------------------------------------------


def test_record_cycle_appends_entry() -> None:
    store = DashboardStore()
    store.record_cycle(duration_seconds=1.5, phase="idle", outcome="success")
    assert len(store.history) == 1
    entry = store.history[0]
    assert entry["phase"] == "idle"
    assert entry["duration_seconds"] == 1.5
    assert entry["outcome"] == "success"
    assert "timestamp" in entry


def test_history_capped_at_20() -> None:
    store = DashboardStore()
    for i in range(25):
        store.record_cycle(duration_seconds=float(i), phase="idle", outcome="success")
    assert len(store.history) == 20


def test_history_newest_first() -> None:
    store = DashboardStore()
    for i in range(3):
        store.record_cycle(duration_seconds=float(i), phase="idle", outcome="success")
    durations = [e["duration_seconds"] for e in store.history]
    assert durations == [2.0, 1.0, 0.0]


def test_record_cycle_updates_last_duration_on_success() -> None:
    store = DashboardStore()
    store.record_cycle(duration_seconds=1.1, phase="idle", outcome="success")
    assert store.last_cycle_duration == 1.1


def test_record_cycle_does_not_update_last_duration_on_error() -> None:
    store = DashboardStore()
    store.record_cycle(duration_seconds=2.0, phase="idle", outcome="success")
    store.record_cycle(duration_seconds=0.0, phase="recovery", outcome="error")
    assert store.last_cycle_duration == 2.0


def test_history_empty_state() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["cycle_history"] == []


# ---------------------------------------------------------------------------
# T031 — SSEBroadcaster
# ---------------------------------------------------------------------------


def test_broadcaster_delivers_to_all_subscribers() -> None:
    async def _run() -> None:
        b = SSEBroadcaster()
        q1 = b.subscribe()
        q2 = b.subscribe()
        b.broadcast({"phase": "idle"})
        p1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        p2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert p1 == {"phase": "idle"}
        assert p2 == {"phase": "idle"}

    asyncio.run(_run())


def test_broadcaster_drops_for_full_queue() -> None:
    """put_nowait on a full queue must not raise — event dropped silently."""
    async def _run() -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        # Fill the queue completely
        for i in range(32):
            q.put_nowait({"i": i})
        # This broadcast should not raise even though queue is full
        b.broadcast({"overflow": True})

    asyncio.run(_run())


def test_broadcaster_unsubscribe_removes_queue() -> None:
    async def _run() -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        b.unsubscribe(q)
        b.broadcast({"phase": "idle"})
        assert q.empty()

    asyncio.run(_run())


def test_broadcaster_concurrent_disconnect_safe() -> None:
    """Modifying _queues during broadcast must not cause RuntimeError."""
    async def _run() -> None:
        b = SSEBroadcaster()
        q1 = b.subscribe()
        q2 = b.subscribe()
        # Remove q2 while we iterate (simulated by clearing set after snapshot)
        # The list() snapshot in broadcast() prevents RuntimeError
        b._queues.discard(q2)
        b.broadcast({"phase": "idle"})  # must not raise
        p1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        assert p1 == {"phase": "idle"}

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# T032 — keepalive comment on timeout
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T005 — build_snapshot() performers-card path (US1 coverage)
# ---------------------------------------------------------------------------


def _make_mock_daemon_with_events(
    phase: str = "monitoring_agent",
    performer_events: list[dict] | None = None,
) -> MagicMock:
    daemon = MagicMock()
    daemon.state = {
        "phase": phase,
        "error_count": 0,
        "performer_events": performer_events or [],
    }
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    return daemon


def test_build_snapshot_includes_performer_events_key() -> None:
    """build_snapshot() must always include performer_events in the output dict."""
    store = DashboardStore()
    daemon = _make_mock_daemon_with_events()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert "performer_events" in snap


def test_build_snapshot_performer_events_empty_when_no_events() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon_with_events(performer_events=[])
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["performer_events"] == []


def test_build_snapshot_performer_events_returned_when_present() -> None:
    events = [
        {"type": "tool_use", "text": "Read: src/app/main.py", "detail": ""},
        {"type": "progress", "text": "Running tests", "detail": ""},
    ]
    store = DashboardStore()
    daemon = _make_mock_daemon_with_events(performer_events=events)
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["performer_events"] == events
    assert len(snap["performer_events"]) == 2


# ---------------------------------------------------------------------------
# T006 — SSE /events endpoint includes performer_events (US1 coverage)
# ---------------------------------------------------------------------------


def test_sse_payload_includes_performer_events_when_state_has_events() -> None:
    """The SSE stream's initial state_update must include performer_events."""
    import json as _json

    events = [{"type": "tool_use", "text": "Edit: app.py", "detail": ""}]

    async def _collect() -> str:
        store = DashboardStore()
        daemon = _make_mock_daemon_with_events(performer_events=events)
        metrics = _make_mock_metrics()
        health = _make_mock_health()
        gen = store.sse_stream(daemon, metrics, health)
        try:
            first_chunk = await gen.__anext__()
        finally:
            await gen.aclose()
        return first_chunk

    first_chunk = asyncio.run(_collect())
    data_line = next(line for line in first_chunk.splitlines() if line.startswith("data:"))
    payload = _json.loads(data_line[len("data:"):].strip())
    assert "performer_events" in payload
    assert payload["performer_events"] == events


# ---------------------------------------------------------------------------
# T032 — keepalive comment on timeout
# ---------------------------------------------------------------------------


def test_sse_generator_yields_keepalive_on_timeout() -> None:
    """When asyncio.wait_for times out, sse_stream() must yield a keepalive comment."""

    async def _get_keepalive() -> str:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        metrics = _make_mock_metrics()
        health = _make_mock_health()

        async def _raise_timeout(coro, timeout=None):  # type: ignore[no-untyped-def]
            coro.close()  # prevent "coroutine was never awaited" warning
            raise TimeoutError

        with patch("coordinare.dashboard.asyncio.wait_for", _raise_timeout):
            gen = store.sse_stream(daemon, metrics, health)
            try:
                await gen.__anext__()  # initial state_update
                keepalive = await gen.__anext__()  # should be keepalive
            finally:
                await gen.aclose()
        return keepalive

    keepalive = asyncio.run(_get_keepalive())
    assert keepalive == ": keepalive\n\n", repr(keepalive)

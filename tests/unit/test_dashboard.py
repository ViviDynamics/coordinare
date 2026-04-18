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
    daemon._cycle_active = False
    daemon.running = True
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


def test_dashboard_html_under_40kb() -> None:
    """T036: _DASHBOARD_HTML must not exceed the 40 KB size budget (raised to accommodate personas panel, 018)."""
    size = len(_DASHBOARD_HTML.encode())
    assert size < 40 * 1024, f"_DASHBOARD_HTML is {size} bytes (limit: {40 * 1024})"


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


def test_build_snapshot_performer_stage_none_coerces_to_empty_string() -> None:
    """Copilot review: if daemon.state["performer_stage"] is explicitly
    None (not missing), ``str(...)`` returns the literal "None" and
    leaks into the dashboard UI as a bogus stage label.  Same bug class
    as the notify and _build_snapshot coercion fixes earlier in this PR."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    # Explicit None — the default "" in .get() does NOT fire for this.
    daemon.state["performer_stage"] = None
    daemon.state["active_sessions"] = {}
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["performer_stage"] == ""
    # And NOT the literal string "None"
    assert snap["performer_stage"] != "None"


def test_build_snapshot_session_performer_stage_none_coerces_to_empty_string() -> None:
    """042: Multi-card active_sessions each carry their own
    performer_stage.  A None value there also must coerce to "" so the
    per-session UI doesn't show "None" as an actual stage."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["active_sessions"] = {
        "CARD_A": {
            "phase": "dispatching",
            "performer_stage": None,  # explicit None
            "current_card": {"title": "Card A"},
        },
    }
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    sessions = snap.get("active_sessions", [])
    assert len(sessions) == 1
    assert sessions[0]["performer_stage"] == ""
    assert sessions[0]["performer_stage"] != "None"


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
    daemon._cycle_active = False
    daemon.running = True
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


# ---------------------------------------------------------------------------
# Broadcaster.shutdown() — sends None sentinel to all subscriber queues
# ---------------------------------------------------------------------------


def test_broadcaster_shutdown_sends_none_sentinel() -> None:
    async def _run() -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        b.shutdown()
        sentinel = await asyncio.wait_for(q.get(), timeout=1.0)
        assert sentinel is None

    asyncio.run(_run())


def test_broadcaster_shutdown_sends_sentinel_to_all_queues() -> None:
    async def _run() -> None:
        b = SSEBroadcaster()
        q1 = b.subscribe()
        q2 = b.subscribe()
        b.shutdown()
        s1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        s2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert s1 is None
        assert s2 is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# DashboardStore.shutdown() — delegates to broadcaster
# ---------------------------------------------------------------------------


def test_store_shutdown_delegates_to_broadcaster() -> None:
    store = DashboardStore()
    store.broadcaster = MagicMock()
    store.shutdown()
    store.broadcaster.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# sse_stream() None sentinel exits the generator cleanly
# ---------------------------------------------------------------------------


def test_sse_stream_exits_on_none_sentinel() -> None:
    async def _run() -> list[str]:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        metrics = _make_mock_metrics()
        health = _make_mock_health()
        gen = store.sse_stream(daemon, metrics, health)
        first = await gen.__anext__()  # initial state_update
        # Put the shutdown sentinel into the queue while generator is suspended
        store.broadcaster.shutdown()
        chunks = [first]
        try:
            async for chunk in gen:
                chunks.append(chunk)
        finally:
            await gen.aclose()
        return chunks

    chunks = asyncio.run(_run())
    assert len(chunks) == 1
    assert chunks[0].startswith("event: state_update")


# ---------------------------------------------------------------------------
# build_snapshot() exception fallback for metrics counter
# ---------------------------------------------------------------------------


def test_build_snapshot_cycles_exception_fallback() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    metrics.cycles_completed_total._value.get.side_effect = Exception("prometheus broken")
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["cycles_completed"] == 0


# ---------------------------------------------------------------------------
# build_snapshot() performer_logs from agent_service.get_agent_logs()
# ---------------------------------------------------------------------------


def test_build_snapshot_performer_logs_when_agent_active() -> None:
    store = DashboardStore()
    agent_service = MagicMock()
    agent_service.get_agent_logs.return_value = ["line one", "line two"]
    daemon = _make_mock_daemon()
    daemon.state = {
        "phase": "monitoring_agent",
        "error_count": 0,
        "agent_service": agent_service,
    }
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["performer_logs"] == ["line one", "line two"]


def test_build_snapshot_performer_logs_empty_when_no_agent() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state = {"phase": "idle", "error_count": 0}
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)
    assert snap["performer_logs"] == []


# ---------------------------------------------------------------------------
# GET /events endpoint — returns text/event-stream
# ---------------------------------------------------------------------------


def test_events_endpoint_returns_text_event_stream() -> None:
    """GET /events must return text/event-stream with a state_update event.

    Patches sse_stream to a finite generator so TestClient doesn't hang on
    the 15-second asyncio.wait_for inside the real generator.
    """

    async def _finite_stream(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        yield "event: state_update\ndata: {}\n\n"

    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    app = create_dashboard_app(store, daemon, metrics, health)

    with patch.object(store, "sse_stream", _finite_stream):
        client = TestClient(app)
        resp = client.get("/events")

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "event: state_update" in resp.text


# ---------------------------------------------------------------------------
# GET /api/performer-logs — streaming log endpoint
# ---------------------------------------------------------------------------


def test_performer_logs_endpoint_no_agent_service() -> None:
    daemon = _make_mock_daemon()
    daemon.state = {"phase": "idle", "error_count": 0}
    client = _make_app(daemon=daemon)
    resp = client.get("/api/performer-logs")
    assert resp.status_code == 200
    assert "no performer active" in resp.text


def test_performer_logs_endpoint_streams_initial_logs() -> None:
    """Patch asyncio.sleep to a no-op and make getter raise on the second call
    so the infinite while-loop exits without hanging the test."""
    from unittest.mock import AsyncMock

    agent_service = MagicMock()
    agent_service.get_agent_logs.side_effect = [["alpha", "beta"], RuntimeError("stop")]
    daemon = _make_mock_daemon()
    daemon.state = {
        "phase": "monitoring_agent",
        "error_count": 0,
        "agent_service": agent_service,
    }
    store = DashboardStore()
    app = create_dashboard_app(store, daemon, _make_mock_metrics(), _make_mock_health())
    client = TestClient(app, raise_server_exceptions=False)
    with patch("coordinare.dashboard.asyncio.sleep", AsyncMock()):
        resp = client.get("/api/performer-logs")
    assert resp.status_code == 200
    assert "alpha" in resp.text
    assert "beta" in resp.text


# ---------------------------------------------------------------------------
# sse_stream line 164: broadcast a real payload → in-loop yield fires
# ---------------------------------------------------------------------------


def test_sse_stream_yields_broadcasted_payload() -> None:
    """Line 164: the in-loop yield fires when a real payload is broadcast."""

    async def _run() -> str:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        metrics = _make_mock_metrics()
        health = _make_mock_health()

        gen = store.sse_stream(daemon, metrics, health)
        # Consume the initial state_update (line 157)
        await gen.__anext__()
        # Now broadcast a real payload so the queue has a non-None item
        store.broadcaster.broadcast({"phase": "dispatching"})
        # This triggers line 164
        event = await gen.__anext__()
        await gen.aclose()
        return event

    event = asyncio.run(_run())
    assert "dispatching" in event
    assert event.startswith("event: state_update")


# ---------------------------------------------------------------------------
# build_snapshot line 213-214: metrics.build_info raises → started_at = None
# ---------------------------------------------------------------------------


def test_build_snapshot_started_at_falls_back_to_none_on_exception() -> None:
    """Lines 213-214: if metrics.build_info raises, started_at is None."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    health = _make_mock_health()

    bad_metrics = MagicMock()
    bad_metrics.cycles_completed_total._value.get.return_value = 0
    bad_metrics.build_info.labels.side_effect = RuntimeError("metrics exploded")

    snapshot = store.build_snapshot(daemon, bad_metrics, health)
    assert snapshot.get("daemon_start_time") is None


# ---------------------------------------------------------------------------
# 018 — Personas API endpoint tests (T016, T016b)
# ---------------------------------------------------------------------------


def _make_personas_app(tmp_path, config_path=None):
    """Build a TestClient for the dashboard with personas support.

    When no explicit config_path is given, writes a default config YAML with
    a custom implementer persona so GET /api/personas returns non-default data.
    """
    import yaml

    from coordinare.dashboard import DashboardStore, create_dashboard_app

    store = DashboardStore()
    daemon = _make_mock_daemon()

    _path = config_path or (tmp_path / "config.yaml")
    if config_path is None:
        _path.write_text(yaml.dump({
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 1,
            "github_token": "tok",
            "human_reviewers": ["alice"],
            "personas": {
                "implementer": {"instructions": "custom"},
            },
        }))

    health = _make_mock_health()
    metrics = _make_mock_metrics()
    app = create_dashboard_app(store, daemon, metrics, health, config_path=_path)
    return TestClient(app)


def test_get_personas_returns_list_of_nine_roles(tmp_path) -> None:
    """GET /api/personas returns 9 roles (042: added closer)."""
    client = _make_personas_app(tmp_path)
    res = client.get("/api/personas")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) == 9
    roles = {p["role"] for p in data}
    assert roles == {
        "advocate", "assessor", "architect", "implementer",
        "reviewer", "security", "qa", "tech_writer", "closer",
    }


def test_get_personas_is_default_flags_correct(tmp_path) -> None:
    """T016: implementer has custom instructions → is_default=False; others → True."""
    client = _make_personas_app(tmp_path)
    res = client.get("/api/personas")
    data = {p["role"]: p for p in res.json()}
    assert data["implementer"]["is_default"] is False
    assert data["assessor"]["is_default"] is True


def test_put_persona_valid_instructions_returns_200(tmp_path) -> None:
    """T016: PUT /api/personas/implementer with valid instructions returns 200."""
    import yaml
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "project_name": "Demo",
        "github_org": "acme",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
    }))
    client = _make_personas_app(tmp_path, config_path=config_path)
    res = client.put(
        "/api/personas/implementer",
        json={"instructions": "Write tests first."},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "implementer"
    assert data["instructions"] == "Write tests first."
    assert data["is_default"] is False


def test_put_persona_instructions_too_long_returns_400(tmp_path) -> None:
    """T016: PUT with instructions > 8000 chars returns 400."""
    from coordinare.config import PERSONA_MAX_LENGTH
    client = _make_personas_app(tmp_path)
    res = client.put(
        "/api/personas/implementer",
        json={"instructions": "x" * (PERSONA_MAX_LENGTH + 1)},
    )
    assert res.status_code == 400
    assert "exceed" in res.json()["error"].lower()


def test_put_persona_unknown_role_returns_404(tmp_path) -> None:
    """T016: PUT with unknown role returns 404."""
    client = _make_personas_app(tmp_path)
    res = client.put("/api/personas/wizard", json={"instructions": "Hello."})
    assert res.status_code == 404


def test_delete_persona_returns_204(tmp_path) -> None:
    """T016: DELETE /api/personas/assessor returns 204."""
    import yaml
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "project_name": "Demo",
        "github_org": "acme",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
    }))
    client = _make_personas_app(tmp_path, config_path=config_path)
    res = client.delete("/api/personas/assessor")
    assert res.status_code == 204


def test_delete_persona_unknown_role_returns_404(tmp_path) -> None:
    """T016: DELETE with unknown role returns 404."""
    client = _make_personas_app(tmp_path)
    res = client.delete("/api/personas/wizard")
    assert res.status_code == 404


def test_put_persona_save_oserror_returns_500(tmp_path) -> None:
    """T016: PUT when save_persona raises OSError returns HTTP 500 with error body (D2)."""
    from unittest.mock import patch

    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "project_name": "Demo", "github_org": "acme",
        "github_project_number": 1, "github_token": "tok",
        "human_reviewers": ["alice"],
    }))
    client = _make_personas_app(tmp_path, config_path=config_path)

    with patch(
        "coordinare.services.persona_service.save_persona",
        side_effect=OSError("disk full"),
    ) as mock_save:
        res = client.put(
            "/api/personas/implementer",
            json={"instructions": "Write tests."},
        )
    assert res.status_code == 500
    assert "error" in res.json()
    assert mock_save.called


def test_put_persona_responds_under_two_seconds(tmp_path) -> None:
    """T016b: PUT /api/personas/implementer responds in < 2 seconds (SC-003)."""
    import time

    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "project_name": "Demo",
        "github_org": "acme",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
    }))
    client = _make_personas_app(tmp_path, config_path=config_path)

    t0 = time.perf_counter()
    res = client.put(
        "/api/personas/implementer",
        json={"instructions": "Write tests first."},
    )
    elapsed = time.perf_counter() - t0

    assert res.status_code == 200
    assert elapsed < 2.0, f"PUT /api/personas took {elapsed:.3f}s (> 2s limit)"


# ---------------------------------------------------------------------------
# 031 — Human Override Controls API tests
# ---------------------------------------------------------------------------


def test_skip_role_active_card() -> None:
    """POST /api/skip-role queues skip override when card is active."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["implementing", "reviewing"]
    daemon.state["performer_stage"] = "implementing"
    client = _make_app(daemon=daemon)

    res = client.post("/api/skip-role")

    assert res.status_code == 200
    assert res.json()["status"] == "override_queued"
    assert res.json()["action"] == "skip"
    assert daemon.state["pending_override"] == {"action": "skip"}


def test_skip_role_no_active_card() -> None:
    """POST /api/skip-role returns 400 when no card is active."""
    daemon = _make_mock_daemon(phase="idle")
    client = _make_app(daemon=daemon)

    res = client.post("/api/skip-role")

    assert res.status_code == 400
    assert "No active card" in res.json()["error"]


def test_restart_from_valid_role() -> None:
    """POST /api/restart-from/{role} queues restart override for valid role."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["implementing", "reviewing", "security"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/restart-from/reviewing")

    assert res.status_code == 200
    assert res.json()["action"] == "restart"
    assert res.json()["target_stage"] == "reviewing"
    assert daemon.state["pending_override"] == {"action": "restart", "target_stage": "reviewing"}


def test_restart_from_role_noun() -> None:
    """POST /api/restart-from/{role} accepts role noun and resolves to stage."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["implementing", "architecting", "reviewing"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/restart-from/architect")

    assert res.status_code == 200
    assert res.json()["target_stage"] == "architecting"
    assert daemon.state["pending_override"] == {"action": "restart", "target_stage": "architecting"}


def test_restart_from_invalid_role() -> None:
    """POST /api/restart-from/{role} returns 400 for unknown role."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["implementing", "reviewing"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/restart-from/nonexistent")

    assert res.status_code == 400
    assert "nonexistent" in res.json()["error"]


def test_restart_from_no_active_card() -> None:
    """POST /api/restart-from/{role} returns 400 when no card is active."""
    daemon = _make_mock_daemon(phase="idle")
    daemon.state["lifecycle_sequence"] = ["implementing"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/restart-from/implementing")

    assert res.status_code == 400


def test_veto_active_card() -> None:
    """POST /api/veto queues veto override when card is active."""
    daemon = _make_mock_daemon(phase="dispatching")
    client = _make_app(daemon=daemon)

    res = client.post("/api/veto")

    assert res.status_code == 200
    assert res.json()["action"] == "veto"
    assert daemon.state["pending_override"] == {"action": "veto"}


def test_veto_no_active_card() -> None:
    """POST /api/veto returns 400 when no card is active."""
    daemon = _make_mock_daemon(phase="idle")
    client = _make_app(daemon=daemon)

    res = client.post("/api/veto")

    assert res.status_code == 400


def test_skip_role_monitoring_agent_phase() -> None:
    """POST /api/skip-role works during monitoring_agent phase."""
    daemon = _make_mock_daemon(phase="monitoring_agent")
    daemon.state["lifecycle_sequence"] = ["implementing", "reviewing"]
    client = _make_app(daemon=daemon)

    res = client.post("/api/skip-role")

    assert res.status_code == 200
    assert daemon.state["pending_override"] == {"action": "skip"}


# ---------------------------------------------------------------------------
# 034 — Cost & Token Tracking dashboard tests
# ---------------------------------------------------------------------------


def test_dashboard_html_includes_token_elements() -> None:
    """Dashboard HTML includes card-tokens-total and card-cost-estimate elements."""
    client = _make_app()
    res = client.get("/")
    assert res.status_code == 200
    assert "card-tokens-total" in res.text
    assert "card-cost-estimate" in res.text
    assert "Card Tokens" in res.text
    assert "Estimated Cost" in res.text


# --- 046: Dependency state in dashboard snapshot ---


def test_build_snapshot_contains_blocked_by_dependencies() -> None:
    """046 T027: The dashboard snapshot includes blocked_by_dependencies
    so the frontend can render blocker badges with issue links."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["blocked_by_dependencies"] = [
        {"issue_number": 42, "title": "Set up theming", "column": "IN_PROGRESS",
         "issue_url": "https://github.com/o/r/issues/42", "source": "explicit"},
    ]
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert "blocked_by_dependencies" in snap
    deps = snap["blocked_by_dependencies"]
    assert len(deps) == 1
    assert deps[0]["issue_number"] == 42
    assert deps[0]["column"] == "IN_PROGRESS"
    assert deps[0]["source"] == "explicit"


def test_build_snapshot_blocked_by_dependencies_empty_when_none() -> None:
    """046: When no dependencies are blocking, the field is an empty list."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["blocked_by_dependencies"] == []


# --- 047: Rebase status in dashboard snapshot ---


def test_build_snapshot_contains_last_rebase_round() -> None:
    """047 T032: Dashboard snapshot includes last_rebase_round."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    daemon.state["last_rebase_round"] = {
        "trigger_pr_number": 42,
        "trigger_sha": "abc123",
        "timestamp": "2026-04-17T12:00:00Z",
        "jobs": [
            {"card_id": "CARD_A", "branch": "coordinare/PVTI_A/feat", "outcome": "clean",
             "post_rebase_sha": "def456", "conflicted_files": [], "duration_seconds": 5.0},
        ],
    }
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert "last_rebase_round" in snap
    rr = snap["last_rebase_round"]
    assert rr["trigger_pr_number"] == 42
    assert len(rr["jobs"]) == 1
    assert rr["jobs"][0]["outcome"] == "clean"


def test_build_snapshot_last_rebase_round_none_when_absent() -> None:
    """047: When no rebase has occurred, the field is None."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health()
    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["last_rebase_round"] is None

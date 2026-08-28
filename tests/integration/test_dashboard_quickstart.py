"""Integration tests: 010-web-dashboard quickstart scenarios.

Maps to the 7 scenarios in specs/010-web-dashboard/quickstart.md.
Tests exercise the DashboardStore → build_snapshot → SSE pipeline at the
integration level — verifying that the correct data is assembled and pushed
through the SSE stream under each operating scenario.

Note: GET / always returns the static _DASHBOARD_HTML template.  Content is
rendered client-side by JavaScript via EventSource.  Assertions are therefore
made against the SSE *payload* (server-side data), not the rendered DOM.
"""
from __future__ import annotations

import asyncio
import json
import socket
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog.testing
from fastapi.testclient import TestClient

from coordinare.daemon import CoordinareDaemon, RuntimeExecutionError
from coordinare.dashboard import (
    DashboardStore,
    check_port_available,
    create_dashboard_app,
)

# ---------------------------------------------------------------------------
# Shared helpers
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
    metrics.cycles_completed_total._value.get.return_value = cycles
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


def _make_monitoring_snapshot() -> MagicMock:
    """WorkflowSnapshot-like mock for an active monitoring_agent card."""
    snap = MagicMock()
    snap.phase = "monitoring_agent"
    snap.active_card_title = "Implement API Timeout"
    snap.active_card_column = "In Progress"
    snap.pr_url = "https://github.com/org/repo/pull/42"
    snap.agent_session_id = "sess-abc123"
    snap.open_questions = []
    return snap


def _make_blocked_snapshot() -> MagicMock:
    """WorkflowSnapshot-like mock for a blocked card with open questions."""
    snap = MagicMock()
    snap.phase = "blocked"
    snap.active_card_title = "Refactor Auth Module"
    snap.active_card_column = "Blocked"
    snap.pr_url = None
    snap.agent_session_id = "sess-xyz789"
    snap.open_questions = [
        "Should we use OAuth2 or API keys?",
        "Which teams need access to this endpoint?",
    ]
    return snap


async def _no_sleep(_: int) -> None:
    return None


def _parse_sse_payload(raw: str) -> dict:
    """Extract and parse the JSON payload from a raw SSE chunk."""
    data_line = next(line for line in raw.splitlines() if line.startswith("data:"))
    return json.loads(data_line[len("data:") :].strip())


# ---------------------------------------------------------------------------
# Scenario 1: Idle daemon
# ---------------------------------------------------------------------------


def test_s1_idle_snapshot_has_no_active_card() -> None:
    """S1: Idle daemon snapshot has no active card, empty history, phase='idle'."""
    store = DashboardStore()
    daemon = _make_mock_daemon(phase="idle")
    daemon.state_store.last_snapshot = None
    metrics = _make_mock_metrics(cycles=0)
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["phase"] == "idle"
    assert snap["phase_label"] == "Idle"
    assert snap["active_card_title"] is None
    assert snap["active_card_column"] is None
    assert snap["pr_url"] is None
    assert snap["agent_session_id"] is None
    assert snap["open_questions"] == []
    assert snap["cycle_history"] == []
    assert snap["cycles_completed"] == 0


def test_s1_idle_all_subsystems_healthy() -> None:
    """S1: All registered subsystems appear healthy in the snapshot."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health(
        probes=[
            {
                "name": "github",
                "status": "healthy",
                "required": True,
                "checked_at": "2026-03-02T10:00:00+00:00",
                "details": None,
            },
            {
                "name": "agent_ssh",
                "status": "healthy",
                "required": True,
                "checked_at": "2026-03-02T10:00:00+00:00",
                "details": None,
            },
        ]
    )

    snap = store.build_snapshot(daemon, metrics, health)

    assert all(s["status"] == "healthy" for s in snap["subsystems"])
    names = {s["name"] for s in snap["subsystems"]}
    assert names == {"github", "agent_ssh"}


def test_s1_html_has_required_structure() -> None:
    """S1: GET / returns HTML with all required element IDs and EventSource setup."""
    store = DashboardStore()
    client = TestClient(
        create_dashboard_app(store, _make_mock_daemon(), _make_mock_metrics(), _make_mock_health()),
        base_url="http://127.0.0.1:8090",
    )

    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Structural IDs that JavaScript writes state into
    for element_id in (
        "phase",
        "active-work-card",
        "questions-card",
        "cycles-completed",
        "last-duration",
        "error-count",
        "daemon-start-time",
        "subsystems-section",
        "history-section",
        "disconnected-banner",
    ):
        assert f'id="{element_id}"' in html, f"HTML is missing id={element_id!r}"

    # SSE connection wiring
    assert "EventSource('/events')" in html
    assert "state_update" in html


# ---------------------------------------------------------------------------
# Scenario 2: Active card in monitoring phase
# ---------------------------------------------------------------------------


def test_s2_active_card_snapshot_fields() -> None:
    """S2: Snapshot contains card title, column, PR URL, and agent session."""
    store = DashboardStore()
    daemon = _make_mock_daemon(phase="monitoring_agent", snapshot=_make_monitoring_snapshot())
    metrics = _make_mock_metrics(cycles=3)
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["phase"] == "monitoring_agent"
    assert snap["phase_label"] == "Monitoring Agent"
    assert snap["active_card_title"] == "Implement API Timeout"
    assert snap["active_card_column"] == "In Progress"
    assert snap["pr_url"] == "https://github.com/org/repo/pull/42"
    assert snap["agent_session_id"] == "sess-abc123"
    assert snap["cycles_completed"] == 3


def test_s2_sse_initial_event_contains_card_data() -> None:
    """S2: First SSE event pushed to a new subscriber includes active card data."""

    async def _collect() -> dict:
        store = DashboardStore()
        daemon = _make_mock_daemon(snapshot=_make_monitoring_snapshot())
        gen = store.sse_stream(daemon, _make_mock_metrics(), _make_mock_health())
        try:
            raw = await gen.__anext__()
        finally:
            await gen.aclose()
        return _parse_sse_payload(raw)

    payload = asyncio.run(_collect())
    assert payload["active_card_title"] == "Implement API Timeout"
    assert payload["pr_url"] == "https://github.com/org/repo/pull/42"
    assert payload["agent_session_id"] == "sess-abc123"
    assert payload["phase"] == "monitoring_agent"


# ---------------------------------------------------------------------------
# Scenario 3: Blocked phase with open questions
# ---------------------------------------------------------------------------


def test_s3_blocked_snapshot_has_open_questions() -> None:
    """S3: Blocked phase snapshot includes the list of open questions."""
    store = DashboardStore()
    daemon = _make_mock_daemon(phase="blocked", snapshot=_make_blocked_snapshot())
    metrics = _make_mock_metrics()
    health = _make_mock_health()

    snap = store.build_snapshot(daemon, metrics, health)

    assert snap["phase"] == "blocked"
    assert snap["phase_label"] == "Blocked"
    assert snap["active_card_title"] == "Refactor Auth Module"
    assert len(snap["open_questions"]) == 2
    assert "OAuth2" in snap["open_questions"][0]
    assert snap["agent_session_id"] == "sess-xyz789"


def test_s3_sse_delivers_open_questions() -> None:
    """S3: SSE initial event for a blocked card delivers the questions list."""

    async def _collect() -> dict:
        store = DashboardStore()
        daemon = _make_mock_daemon(snapshot=_make_blocked_snapshot())
        gen = store.sse_stream(daemon, _make_mock_metrics(), _make_mock_health())
        try:
            raw = await gen.__anext__()
        finally:
            await gen.aclose()
        return _parse_sse_payload(raw)

    payload = asyncio.run(_collect())
    assert payload["phase"] == "blocked"
    assert len(payload["open_questions"]) == 2


# ---------------------------------------------------------------------------
# Scenario 4: Subsystem degraded
# ---------------------------------------------------------------------------


def test_s4_degraded_subsystem_distinct_from_healthy() -> None:
    """S4: Degraded subsystem has status='degraded'; other subsystems are unaffected."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health(
        probes=[
            {
                "name": "github",
                "status": "degraded",
                "required": True,
                "checked_at": "2026-03-02T10:00:00+00:00",
                "details": "circuit open: github",
            },
            {
                "name": "agent_ssh",
                "status": "healthy",
                "required": True,
                "checked_at": "2026-03-02T10:00:00+00:00",
                "details": None,
            },
        ]
    )

    snap = store.build_snapshot(daemon, metrics, health)

    by_name = {s["name"]: s for s in snap["subsystems"]}
    assert by_name["github"]["status"] == "degraded"
    assert by_name["github"]["details"] == "circuit open: github"
    assert by_name["github"]["required"] is True
    assert by_name["agent_ssh"]["status"] == "healthy"


def test_s4_unavailable_optional_subsystem() -> None:
    """S4: Unconfigured optional subsystem (e.g. notifications) appears as 'unavailable'."""
    store = DashboardStore()
    daemon = _make_mock_daemon()
    metrics = _make_mock_metrics()
    health = _make_mock_health(
        probes=[
            {
                "name": "notifications",
                "status": "unavailable",
                "required": False,
                "checked_at": "2026-03-02T10:00:00+00:00",
                "details": "not configured",
            }
        ]
    )

    snap = store.build_snapshot(daemon, metrics, health)

    notif = next(s for s in snap["subsystems"] if s["name"] == "notifications")
    assert notif["status"] == "unavailable"
    assert notif["required"] is False
    assert notif["details"] == "not configured"


# ---------------------------------------------------------------------------
# Scenario 5: Cycle history accumulation via real CoordinareDaemon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s5_success_cycles_build_history() -> None:
    """S5: Five successful daemon cycles produce five newest-first history entries."""

    class _SuccessGraph:
        async def ainvoke(self, state: dict) -> dict:
            state["phase"] = "monitoring_agent"
            return state

    store = DashboardStore()
    daemon = CoordinareDaemon(
        _SuccessGraph(),
        max_cycles=5,
        sleep_func=_no_sleep,
        dashboard_store=store,
    )
    await daemon.start()

    assert len(store.history) == 5
    for entry in store.history:
        assert entry["outcome"] == "success"
        assert entry["phase"] == "monitoring_agent"
        assert "timestamp" in entry
        assert entry["duration_seconds"] >= 0.0

    assert store.last_cycle_duration is not None
    assert store.last_cycle_duration >= 0.0


@pytest.mark.asyncio
async def test_s5_mixed_success_and_error_cycles() -> None:
    """S5: Two success cycles followed by a graph error produces correct history order."""

    class _FailOnThird:
        def __init__(self) -> None:
            self._count = 0

        async def ainvoke(self, state: dict) -> dict:
            self._count += 1
            if self._count == 3:
                raise RuntimeError("simulated agent failure")
            state["phase"] = "idle"
            return state

    store = DashboardStore()
    daemon = CoordinareDaemon(
        _FailOnThird(),
        max_cycles=5,  # stops early at cycle 3 due to exception
        sleep_func=_no_sleep,
        dashboard_store=store,
    )

    with pytest.raises(RuntimeExecutionError):
        await daemon.start()

    # 2 success + 1 error; deque is newest-first
    assert len(store.history) == 3
    outcomes = [e["outcome"] for e in store.history]
    assert outcomes[0] == "error"
    assert outcomes[1] == "success"
    assert outcomes[2] == "success"

    # last_cycle_duration is from the last *successful* cycle, not the error
    assert store.last_cycle_duration is not None


@pytest.mark.asyncio
async def test_s5_history_capped_at_20() -> None:
    """S5: History is bounded at 20 entries — oldest dropped when full."""

    class _FastGraph:
        async def ainvoke(self, state: dict) -> dict:
            state["phase"] = "idle"
            return state

    store = DashboardStore()
    daemon = CoordinareDaemon(
        _FastGraph(), max_cycles=25, sleep_func=_no_sleep, dashboard_store=store
    )
    await daemon.start()

    assert len(store.history) == 20


# ---------------------------------------------------------------------------
# Scenario 6: Port conflict causes immediate exit with structured log
# ---------------------------------------------------------------------------


def test_s6_port_conflict_exits_1_with_structured_log() -> None:
    """S6: check_port_available logs dashboard_port_conflict and calls sys.exit(1)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

        with structlog.testing.capture_logs() as cap_logs, pytest.raises(SystemExit) as exc_info:
            check_port_available("127.0.0.1", port)

    assert exc_info.value.code == 1

    conflict_events = [e for e in cap_logs if e.get("event") == "dashboard_port_conflict"]
    assert conflict_events, (
        f"Expected dashboard_port_conflict log; got: {[e.get('event') for e in cap_logs]}"
    )
    ev = conflict_events[0]
    assert ev["port"] == port
    assert ev.get("log_level") == "error"


# ---------------------------------------------------------------------------
# Scenario 7: SSE live updates and reconnect behaviour
# ---------------------------------------------------------------------------


def test_s7_broadcast_arrives_as_second_sse_event() -> None:
    """S7: After initial snapshot, a daemon broadcast arrives as the next SSE event."""

    async def _run() -> tuple[dict, dict]:
        store = DashboardStore()
        daemon = _make_mock_daemon(phase="idle")
        gen = store.sse_stream(daemon, _make_mock_metrics(), _make_mock_health())
        try:
            first_raw = await gen.__anext__()

            # Simulate daemon completing a cycle → broadcasts updated state
            updated = {"phase": "dispatching", "phase_label": "Dispatching"}
            store.broadcaster.broadcast(updated)

            # Queue already has the item — wait_for returns immediately
            second_raw = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        finally:
            await gen.aclose()

        return _parse_sse_payload(first_raw), _parse_sse_payload(second_raw)

    first_payload, second_payload = asyncio.run(_run())

    assert first_payload["phase"] == "idle"
    assert second_payload["phase"] == "dispatching"
    assert second_payload["phase_label"] == "Dispatching"


def test_s7_multiple_open_tabs_receive_same_broadcast() -> None:
    """S7: A single broadcast fans out to all subscribed SSE clients (open tabs)."""

    async def _run() -> tuple[dict, dict]:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        metrics = _make_mock_metrics()
        health = _make_mock_health()

        gen1 = store.sse_stream(daemon, metrics, health)
        gen2 = store.sse_stream(daemon, metrics, health)
        try:
            await gen1.__anext__()  # consume initial snapshots
            await gen2.__anext__()

            # One broadcast → both clients receive it
            broadcast_payload = {"phase": "merging", "phase_label": "Merging"}
            store.broadcaster.broadcast(broadcast_payload)

            ev1 = await asyncio.wait_for(gen1.__anext__(), timeout=1.0)
            ev2 = await asyncio.wait_for(gen2.__anext__(), timeout=1.0)
        finally:
            await gen1.aclose()
            await gen2.aclose()

        return _parse_sse_payload(ev1), _parse_sse_payload(ev2)

    p1, p2 = asyncio.run(_run())
    assert p1["phase"] == "merging"
    assert p2["phase"] == "merging"


def test_s7_sse_stream_cleans_up_on_disconnect() -> None:
    """S7: When a client disconnects (aclose), its queue is removed from broadcaster."""

    async def _run() -> int:
        store = DashboardStore()
        daemon = _make_mock_daemon()
        gen = store.sse_stream(daemon, _make_mock_metrics(), _make_mock_health())

        await gen.__anext__()  # subscribe
        subscribers_after_connect = len(store.broadcaster._queues)

        await gen.aclose()  # triggers finally → unsubscribe
        subscribers_after_disconnect = len(store.broadcaster._queues)

        return subscribers_after_connect, subscribers_after_disconnect  # type: ignore[return-value]

    after_connect, after_disconnect = asyncio.run(_run())
    assert after_connect == 1
    assert after_disconnect == 0


def test_s7_daemon_restart_cycle_resets_history() -> None:
    """S7: A fresh DashboardStore after restart shows empty history and new start time."""
    # Original store from first run
    store1 = DashboardStore()
    store1.record_cycle(duration_seconds=1.5, phase="idle", outcome="success")
    assert len(store1.history) == 1

    # Simulated restart — new store instance (as __main__ creates on each run)
    store2 = DashboardStore()
    assert len(store2.history) == 0
    assert store2.last_cycle_duration is None

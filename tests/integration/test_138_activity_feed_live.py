"""138 US1: agent activity reaches the feed in the invocation that observed it.

T020 (attribution + same-invocation), T021 (wedged agent), T022 (stage change),
T025 (live sink fan-out on an already-open connection).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from coordinare.dashboard import DashboardStore
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state
from coordinare.services.activity_log import ActivityLog


class _Performer:
    """Backend returning a fixed status — including a re-reported event list."""

    def __init__(self, response: dict) -> None:
        self._response = response
        self.polls = 0

    async def check_status(self, session_id: str, **_: object) -> dict:
        _ = session_id
        self.polls += 1
        return dict(self._response)


def _state_with_log(events: list[dict], log: ActivityLog) -> dict:
    state = initial_state()
    state["performer_services"] = {"implementing": _Performer({
        "status": "working",
        "events": events,
    })}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {
        "id": "CARD_X",
        "status": "IN_PROGRESS",
        "title": "Add retry budget to the closer",
        "issue_number": 142,
    }
    state["agent_dispatch"] = {"session_id": "sess-1"}
    state["activity_log"] = log
    return state


_EVENTS = [
    {"type": "tool_use", "text": "Edit src/coordinare/services/notify.py"},
    {"type": "thinking", "text": "Considering the retry budget"},
    {"type": "cost", "text": "12345 tokens"},
]


# ---------------------------------------------------------------------------
# T020 — pushed at the observation site, fully attributed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_events_reach_the_feed_in_the_same_invocation() -> None:
    """FR-005, SC-004: no cycle wait, and every attribution field is present."""
    log = ActivityLog()
    await monitor_performer(_state_with_log(_EVENTS, log))

    entries = log.snapshot()
    assert [e["activity_type"] for e in entries] == ["tool_use", "thinking", "cost"]
    for entry in entries:
        assert entry["card_id"] == "CARD_X"
        assert entry["card_number"] == 142
        assert entry["card_title"] == "Add retry budget to the closer"
        assert entry["stage"] == "implementing"
        assert entry["timestamp"]
    assert entries[0]["text"] == "Edit src/coordinare/services/notify.py"


@pytest.mark.asyncio
async def test_missing_activity_log_is_a_noop() -> None:
    """Every call site must tolerate state.get("activity_log") being None."""
    state = _state_with_log(_EVENTS, ActivityLog())
    state["activity_log"] = None
    result = await monitor_performer(state)
    assert result["performer_events"]


# ---------------------------------------------------------------------------
# T021 — a wedged agent must never look busy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wedged_agent_produces_no_new_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """SC-009: a backend re-reporting its accumulated list adds nothing.

    The push site hands the whole reported list to record_many without
    pre-diffing — suppression is the log's job (FR-022, FR-023).
    """
    log = ActivityLog()
    state = _state_with_log(_EVENTS, log)
    for _ in range(25):
        state = await monitor_performer(state)
    assert len(log.snapshot()) == len(_EVENTS)


# ---------------------------------------------------------------------------
# T022 — stage transitions, derived by the watcher
# ---------------------------------------------------------------------------


def _watcher_daemon(sessions: dict) -> MagicMock:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0, "active_sessions": sessions}
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    return daemon


def _mock_metrics() -> MagicMock:
    m = MagicMock()
    m.cycles_completed_total._value.get.return_value = 0
    m.build_info.labels.return_value._value.get.return_value = {"started_at": "2026-07-30T09:30:00+00:00"}
    return m


def _mock_health() -> MagicMock:
    h = MagicMock()
    h.snapshot.return_value.probes = []
    return h


@pytest.mark.asyncio
async def test_stage_change_recorded_within_one_watcher_tick() -> None:
    """FR-006: one tick after the writeback — stage changes are cycle-granular."""
    sessions = {
        "CARD_A": {
            "current_card": {"id": "CARD_A", "title": "First card", "issue_number": 7},
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
        },
    }
    store = DashboardStore()
    daemon = _watcher_daemon(sessions)
    store._watcher_fingerprint = store._active_sessions_fingerprint(daemon)
    task = asyncio.create_task(
        store._watch_active_sessions(daemon, _mock_metrics(), _mock_health()),
    )
    try:
        sessions["CARD_A"]["performer_stage"] = "reviewing"
        await asyncio.sleep(0.35)
    finally:
        task.cancel()

    entries = [e for e in store.activity_log.snapshot() if e["activity_type"] == "stage_change"]
    assert entries, "no stage_change recorded"
    assert entries[-1]["stage"] == "reviewing"
    assert entries[-1]["card_id"] == "CARD_A"
    assert entries[-1]["card_number"] == 7
    assert "reviewing" in entries[-1]["text"]


@pytest.mark.asyncio
async def test_departed_card_releases_dedup_bookkeeping_but_keeps_entries() -> None:
    """T026 / FR-024."""
    sessions = {
        "CARD_A": {
            "current_card": {"id": "CARD_A", "title": "First", "issue_number": 7},
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
        },
    }
    store = DashboardStore()
    daemon = _watcher_daemon(sessions)
    store._record_stage_changes(daemon, None, store._active_sessions_fingerprint(daemon))
    assert "CARD_A" in store.activity_log._seen

    previous = store._active_sessions_fingerprint(daemon)
    sessions.clear()
    store._record_stage_changes(daemon, previous, store._active_sessions_fingerprint(daemon))
    assert "CARD_A" not in store.activity_log._seen
    assert len(store.activity_log.snapshot()) == 1


# ---------------------------------------------------------------------------
# T025 — the sink: live fan-out on an already-open connection
# ---------------------------------------------------------------------------


async def _open_stream(store: DashboardStore, daemon: MagicMock):
    gen = store.sse_stream(daemon, _mock_metrics(), _mock_health())
    await gen.__anext__()   # initial state_update
    return gen


async def _next_or_none(gen, timeout: float = 0.3) -> str | None:
    try:
        return await asyncio.wait_for(gen.__anext__(), timeout=timeout)
    except TimeoutError:
        return None


@pytest.mark.asyncio
async def test_graph_node_push_reaches_an_open_connection_without_reconnect() -> None:
    """One record from a graph node → one activity_event, no reconnect."""
    store = DashboardStore()
    daemon = _watcher_daemon({})
    gen = await _open_stream(store, daemon)
    try:
        state = _state_with_log(_EVENTS, store.activity_log)
        await monitor_performer(state)
        message = await _next_or_none(gen, timeout=1.0)
    finally:
        await gen.aclose()

    assert message is not None, "pushed entries never reached the open stream"
    assert message.startswith("event: activity_event")
    payload = json.loads(message.split("data: ", 1)[1])
    assert len(payload["entries"]) == len(_EVENTS)


@pytest.mark.asyncio
async def test_watcher_tick_arrives_as_exactly_one_activity_event() -> None:
    sessions = {
        "CARD_A": {
            "current_card": {"id": "CARD_A", "title": "First", "issue_number": 7},
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
        },
        "CARD_B": {
            "current_card": {"id": "CARD_B", "title": "Second", "issue_number": 8},
            "phase": "monitoring_performer",
            "performer_stage": "reviewing",
        },
    }
    store = DashboardStore()
    daemon = _watcher_daemon(sessions)
    gen = await _open_stream(store, daemon)
    try:
        store._record_stage_changes(daemon, None, store._active_sessions_fingerprint(daemon))
        message = await _next_or_none(gen, timeout=1.0)
        assert message is not None and message.startswith("event: activity_event")
        payload = json.loads(message.split("data: ", 1)[1])
        assert len(payload["entries"]) == 2   # one event, both cards
        # A tick that records nothing emits nothing.
        store._record_stage_changes(
            daemon,
            store._active_sessions_fingerprint(daemon),
            store._active_sessions_fingerprint(daemon),
        )
        assert await _next_or_none(gen) is None
    finally:
        await gen.aclose()

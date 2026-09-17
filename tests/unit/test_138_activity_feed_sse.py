"""138 T005: wire-contract tests for the activity_event SSE stream.

Contract: specs/138-dashboard-activity-feed/contracts/activity-event.md §1.6
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

from coordinare.dashboard import DashboardStore

# ---------------------------------------------------------------------------
# Mock daemon/metrics/health — mirrors tests/unit/test_dashboard.py helpers
# ---------------------------------------------------------------------------


def _make_mock_daemon() -> MagicMock:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    return daemon


def _make_mock_metrics() -> MagicMock:
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": "2026-07-30T09:30:00+00:00",
    }
    return metrics


def _make_mock_health() -> MagicMock:
    health = MagicMock()
    probe = MagicMock()
    probe.subsystem_name = "github"
    probe.status.value = "healthy"
    probe.is_required = True
    probe.checked_at.isoformat.return_value = "2026-07-30T10:00:00+00:00"
    probe.details = None
    health.snapshot.return_value.probes = [probe]
    return health


def _parse(message: str) -> tuple[str, dict]:
    """Split an SSE frame into (event name, decoded data)."""
    lines = message.strip().split("\n")
    name = lines[0].removeprefix("event: ")
    data = json.loads(lines[1].removeprefix("data: "))
    return name, data


async def _drain(store: DashboardStore, count: int, *, before_read: Any = None) -> list[str]:
    daemon, metrics, health = _make_mock_daemon(), _make_mock_metrics(), _make_mock_health()
    gen = store.sse_stream(daemon, metrics, health)
    out: list[str] = []
    try:
        for i in range(count):
            if before_read is not None and i == before_read[0]:
                before_read[1]()
            out.append(await asyncio.wait_for(gen.__anext__(), timeout=2.0))
    finally:
        await gen.aclose()
    return out


# ---------------------------------------------------------------------------
# FR-002: state_update is additive-only
# ---------------------------------------------------------------------------

# The exact snapshot surface before spec 138, with the type each key carries.
_PRE_138_KEYS: dict[str, Any] = {
    "phase": str,
    "phase_label": str,
    "active_card_title": type(None),
    "active_card_column": type(None),
    "active_card_issue_url": type(None),
    "pr_url": type(None),
    "agent_session_id": type(None),
    "agent_dispatch_at": type(None),
    "open_questions": list,
    "card_clarifications": list,
    "performer_events": list,
    "performer_metrics": type(None),
    "performer_stage": str,
    "lifecycle_sequence": list,
    "performer_backend": type(None),
    "performer_logs": list,
    "card_tokens_total": int,
    "card_cost_estimate": (int, float),
    "active_session_count": int,
    "active_sessions": list,
    "subsystems": list,
    "overall_health": str,
    "project_name": str,
    "project_board_url": str,
    "cycles_completed": int,
    "last_cycle_duration_seconds": type(None),
    "consecutive_error_count": int,
    "daemon_start_time": str,
    "cycle_history": list,
    "cycle_active": bool,
    "daemon_running": bool,
    "blocked_by_dependencies": list,
    "last_rebase_round": type(None),
    "role_utilization": list,
    "assignee_filter": type(None),
    "board_summary": dict,
    "last_poll_at": type(None),
    "backend_ui_url": type(None),
    "session_stats": type(None),
    "session_skip_reasons": dict,
    "symphonies": list,
    "coordinare": dict,
}


def test_state_update_shape_additive_only() -> None:
    """Every pre-138 key survives unchanged; exactly one key is added (T016)."""
    snapshot = DashboardStore().build_snapshot(
        _make_mock_daemon(), _make_mock_metrics(), _make_mock_health(),
    )
    missing = set(_PRE_138_KEYS) - set(snapshot)
    assert not missing, f"pre-138 keys removed: {sorted(missing)}"
    added = set(snapshot) - set(_PRE_138_KEYS)
    # 160 added include_unassigned and ownership_hint beside the pre-existing
    # assignee_filter. 343 added stall_timeout_seconds, which the step trail
    # uses to decide when a step has sat too long -- the per-step fields
    # themselves live on each active_sessions entry, not at top level.
    assert added == {
        "activity_quiet_threshold_seconds",
        "include_unassigned",
        "ownership_hint",
        "stall_timeout_seconds",
    }, f"unplanned additions: {sorted(added)}"
    for key, expected in _PRE_138_KEYS.items():
        assert isinstance(snapshot[key], expected), f"{key} changed type"


def test_ownership_hint_is_computed_from_the_daemon_config() -> None:
    """160 FR-010: the snapshot carries the policy, not the raw fields alone.

    Pinned here because this is where the snapshot helpers live. Without it, the
    call in build_snapshot could be replaced by a constant "" and every test of
    the hint function itself would stay green.
    """
    daemon = _make_mock_daemon()
    daemon.state["config"] = MagicMock(assignee_filter=None, include_unassigned=True)
    snapshot = DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    assert snapshot["ownership_hint"] == "unassigned"

    daemon.state["config"] = MagicMock(assignee_filter="bot", include_unassigned=False)
    snapshot = DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    assert snapshot["ownership_hint"] == "bot"


def test_state_update_has_no_underscore_event_key() -> None:
    """The _event discriminator can never collide with a snapshot key."""
    snapshot = DashboardStore().build_snapshot(
        _make_mock_daemon(), _make_mock_metrics(), _make_mock_health(),
    )
    assert "_event" not in snapshot


# ---------------------------------------------------------------------------
# activity_event emission
# ---------------------------------------------------------------------------


def test_activity_event_emitted_with_entries() -> None:
    store = DashboardStore()

    async def _run() -> list[str]:
        def _push() -> None:
            store.activity_log.record(
                activity_type="tool_use", card_id="C1", stage="implementing", text="Edit x.py",
            )

        return await _drain(store, 2, before_read=(1, _push))

    messages = asyncio.run(_run())
    name, data = _parse(messages[1])
    assert name == "activity_event"
    assert data["_event"] == "activity_event"
    assert len(data["entries"]) == 1
    assert data["entries"][0]["activity_type"] == "tool_use"
    assert data["entries"][0]["card_id"] == "C1"


def test_backfill_follows_initial_state_update() -> None:
    """FR-003: state_update stays first; backfill is second."""
    store = DashboardStore()
    store.activity_log.record(activity_type="progress", card_id="C1", text="earlier")

    messages = asyncio.run(_drain(store, 2))
    assert _parse(messages[0])[0] == "state_update"
    name, data = _parse(messages[1])
    assert name == "activity_event"
    assert [e["text"] for e in data["entries"]] == ["earlier"]


def test_no_backfill_when_log_is_empty() -> None:
    """Invariant 2: an activity_event is never emitted with nothing to say."""
    store = DashboardStore()

    async def _run() -> str:
        daemon, metrics, health = _make_mock_daemon(), _make_mock_metrics(), _make_mock_health()

        async def _raise_timeout(coro, timeout=None):  # type: ignore[no-untyped-def]
            coro.close()
            raise TimeoutError

        gen = store.sse_stream(daemon, metrics, health)
        try:
            await gen.__anext__()
            with patch("coordinare.dashboard.asyncio.wait_for", _raise_timeout):
                return await gen.__anext__()
        finally:
            await gen.aclose()

    assert asyncio.run(_run()) == ": keepalive\n\n"


def test_entries_ordered_oldest_first() -> None:
    store = DashboardStore()
    for i in range(5):
        store.activity_log.record(activity_type="progress", card_id="C1", text=f"line {i}")

    messages = asyncio.run(_drain(store, 2))
    _, data = _parse(messages[1])
    seqs = [e["seq"] for e in data["entries"]]
    assert seqs == sorted(seqs)
    assert data["entries"][0]["text"] == "line 0"


# ---------------------------------------------------------------------------
# SC-007: the existing keepalive contract still holds
# ---------------------------------------------------------------------------


def test_keepalive_precedes_heartbeat() -> None:
    store = DashboardStore()

    async def _run() -> list[str]:
        daemon, metrics, health = _make_mock_daemon(), _make_mock_metrics(), _make_mock_health()

        async def _raise_timeout(coro, timeout=None):  # type: ignore[no-untyped-def]
            coro.close()
            raise TimeoutError

        gen = store.sse_stream(daemon, metrics, health)
        out = []
        try:
            await gen.__anext__()  # initial state_update
            with patch("coordinare.dashboard.asyncio.wait_for", _raise_timeout):
                out.append(await gen.__anext__())
                out.append(await gen.__anext__())
        finally:
            await gen.aclose()
        return out

    keepalive, heartbeat = asyncio.run(_run())
    assert keepalive == ": keepalive\n\n"
    assert _parse(heartbeat) == ("heartbeat", {"_event": "heartbeat"})


# ---------------------------------------------------------------------------
# FR-004: backpressure
# ---------------------------------------------------------------------------


def test_slow_client_drops_without_blocking() -> None:
    store = DashboardStore()

    async def _run() -> None:
        q = store.broadcaster.subscribe()
        while not q.full():
            q.put_nowait({"filler": True})
        # A full queue must be a silent drop, not a raise or an await.
        store.activity_log.record(activity_type="progress", card_id="C1", text="dropped")
        assert q.full()

    asyncio.run(asyncio.wait_for(_run(), timeout=2.0))


def test_store_wires_the_log_sink_to_the_broadcaster() -> None:
    """T015: the one place the log and the transport meet."""
    store = DashboardStore()
    assert store.activity_log.sink == store.broadcaster.broadcast_activity

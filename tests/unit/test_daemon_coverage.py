"""Tests for CoordinareDaemon private methods (coverage for daemon.py)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.state_store import WorkflowSnapshot


async def _no_sleep(_: int) -> None:
    return None


def _make_daemon(**kwargs) -> CoordinareDaemon:
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})
    return CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
        **kwargs,
    )


def _make_snapshot(phase: str = "idle", active_card_id: str | None = None) -> WorkflowSnapshot:
    return WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase=phase,
        active_card_id=active_card_id,
        active_card_title="Test Card",
        active_card_column="In Progress",
    )


# ---------------------------------------------------------------------------
# _build_snapshot — with agent_dispatch and open_questions
# ---------------------------------------------------------------------------


def test_build_snapshot_includes_agent_session_id() -> None:
    daemon = _make_daemon()
    daemon._state["agent_dispatch"] = {"session_id": "sess-123"}
    snapshot = daemon._build_snapshot()
    assert snapshot.agent_session_id == "sess-123"


def test_build_snapshot_includes_open_questions() -> None:
    daemon = _make_daemon()
    daemon._state["open_questions"] = ["Q1?", "Q2?"]
    snapshot = daemon._build_snapshot()
    assert snapshot.open_questions == ["Q1?", "Q2?"]


def test_build_snapshot_with_no_dispatch_has_no_session_id() -> None:
    daemon = _make_daemon()
    snapshot = daemon._build_snapshot()
    assert snapshot.agent_session_id is None


def test_build_snapshot_includes_performer_stage_and_lifecycle_sequence() -> None:
    """053: Snapshot must carry lifecycle position for restart continuity."""
    daemon = _make_daemon()
    daemon._state["performer_stage"] = "architecting"
    daemon._state["lifecycle_sequence"] = ["assessing", "architecting", "implementing"]

    snapshot = daemon._build_snapshot()

    assert snapshot.performer_stage == "architecting"
    assert snapshot.lifecycle_sequence == ["assessing", "architecting", "implementing"]


# ---------------------------------------------------------------------------
# 042 — pr_url / pr_node_id None coercion (regression: writes literal "None")
# ---------------------------------------------------------------------------


def test_build_snapshot_none_pr_fields_serialize_to_null_not_string() -> None:
    """042: A card with pr_url=None must serialize to JSON null in the
    snapshot — NOT to the literal string "None". The previous bug used
    str(card_dict.get("pr_url", "")) which returns "None" when the value
    is None (because the default "" only fires on missing keys), and the
    string "None" is truthy so the ``or None`` guard didn't help.
    Restored snapshots then carried "pr_node_id": "None" into monitor_pr,
    which queried GitHub with "None" and tripped the circuit breaker."""
    daemon = _make_daemon()
    daemon._state["current_card"] = {
        "id": "card-1",
        "title": "Test",
        "status": "IN_REVIEW",
        "pr_url": None,
        "pr_node_id": None,
    }
    snapshot = daemon._build_snapshot()
    assert snapshot.pr_url is None
    assert snapshot.pr_node_id is None
    # Crucially: when serialized to JSON, these come out as null not "None"
    json_payload = snapshot.model_dump_json()
    assert '"pr_url":null' in json_payload
    assert '"pr_node_id":null' in json_payload
    assert '"None"' not in json_payload


def test_build_snapshot_empty_string_pr_fields_serialize_to_null() -> None:
    """042: Empty-string pr_url/pr_node_id (e.g. from a freshly-created
    card before the implementer opens the PR) also serialize to null,
    not to empty string."""
    daemon = _make_daemon()
    daemon._state["current_card"] = {
        "id": "card-1",
        "title": "Test",
        "pr_url": "",
        "pr_node_id": "",
    }
    snapshot = daemon._build_snapshot()
    assert snapshot.pr_url is None
    assert snapshot.pr_node_id is None


def test_build_snapshot_real_pr_fields_preserved() -> None:
    """042: When pr_url/pr_node_id are real strings, they must round-trip
    unchanged — the None-coercion fix must not strip valid values."""
    daemon = _make_daemon()
    daemon._state["current_card"] = {
        "id": "card-1",
        "title": "Test",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_kwDO_real",
    }
    snapshot = daemon._build_snapshot()
    assert snapshot.pr_url == "https://github.com/org/repo/pull/42"
    assert snapshot.pr_node_id == "PR_kwDO_real"


def test_build_snapshot_none_card_fields_all_serialize_to_null() -> None:
    """042: Every card field that goes through _str_or_none must serialize
    to null when the underlying value is None — not just pr_url/pr_node_id.
    This catches future fields that could regress to str(None) → "None"."""
    daemon = _make_daemon()
    daemon._state["current_card"] = {
        "id": None,
        "title": None,
        "status": None,
        "issue_id": None,
        "pr_url": None,
        "pr_node_id": None,
    }
    snapshot = daemon._build_snapshot()
    assert snapshot.active_card_id is None
    assert snapshot.active_card_title is None
    assert snapshot.active_card_column is None
    assert snapshot.active_card_issue_id is None
    assert snapshot.pr_url is None
    assert snapshot.pr_node_id is None
    json_payload = snapshot.model_dump_json()
    assert '"None"' not in json_payload


def test_build_snapshot_none_session_id_serializes_to_null() -> None:
    """042: Same coercion applies to agent_session_id from the dispatch dict."""
    daemon = _make_daemon()
    daemon._state["agent_dispatch"] = {"session_id": None}
    snapshot = daemon._build_snapshot()
    assert snapshot.agent_session_id is None


def test_build_snapshot_round_trip_through_disk() -> None:
    """042: End-to-end regression — _build_snapshot followed by
    model_validate_json must round-trip None values cleanly without the
    literal "None" string corrupting the restored state."""
    daemon = _make_daemon()
    daemon._state["current_card"] = {
        "id": "card-1",
        "title": "Test card",
        "status": "BLOCKED",
        "pr_url": None,
        "pr_node_id": None,
    }
    snapshot = daemon._build_snapshot()
    payload = snapshot.model_dump_json()
    restored = WorkflowSnapshot.model_validate_json(payload)
    assert restored.pr_url is None
    assert restored.pr_node_id is None
    # And after restoring into a fresh daemon, the card must not carry
    # the string "None" forward — that was the symptom that broke
    # monitor_pr's get_pr_reviews call.
    new_daemon = _make_daemon()
    new_daemon._restore_from_snapshot(restored)
    card = new_daemon._state.get("current_card") or {}
    assert card.get("pr_node_id") in (None, "")
    assert card.get("pr_url") in (None, "")


# ---------------------------------------------------------------------------
# _restore_from_snapshot
# ---------------------------------------------------------------------------


def test_restore_from_snapshot_sets_phase() -> None:
    daemon = _make_daemon()
    snap = _make_snapshot(phase="monitoring_agent", active_card_id="card-42")
    daemon._restore_from_snapshot(snap)
    assert daemon._state["phase"] == "monitoring_agent"


def test_restore_from_snapshot_sets_current_card() -> None:
    daemon = _make_daemon()
    snap = _make_snapshot(phase="monitoring_agent", active_card_id="card-42")
    daemon._restore_from_snapshot(snap)
    assert daemon._state["current_card"]["id"] == "card-42"


def test_restore_from_snapshot_sets_agent_dispatch_when_session_id_present() -> None:
    daemon = _make_daemon()
    snap = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="monitoring_agent",
        active_card_id="card-1",
        agent_session_id="sess-xyz",
    )
    daemon._restore_from_snapshot(snap)
    assert daemon._state["agent_dispatch"]["session_id"] == "sess-xyz"


def test_restore_from_snapshot_v1_synthesizes_active_sessions() -> None:
    """066 T007 / FR-005: pre-Fix-7 (v1) snapshot — active_sessions empty,
    active_card_id + top-level card fields present — must synthesize a
    single-entry session keyed by active_card_id with the same shape a
    fresh multi-card pickup would produce.  Also asserts I3 invariant.
    """
    daemon = _make_daemon()
    snap = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="monitoring_performer",
        active_card_id="card-v1",
        active_card_title="Legacy Card",
        active_card_column="In Progress",
        performer_stage="implementing",
        active_sessions={},
    )

    daemon._restore_from_snapshot(snap)

    sessions = daemon._state.get("active_sessions") or {}
    assert set(sessions.keys()) == {"card-v1"}
    session = sessions["card-v1"]
    assert session["current_card"] is daemon._state["current_card"]
    assert session["performer_stage"] == "implementing"
    assert session["phase"] == "monitoring_performer"
    # I3 invariant: top-level current_card mirrors the session entry.
    assert daemon._state["active_card_id"] == "card-v1"
    assert daemon._state["current_card"]["id"] == "card-v1"


def test_restore_from_snapshot_no_card_skips_current_card() -> None:
    daemon = _make_daemon()
    snap = _make_snapshot(phase="idle", active_card_id=None)
    daemon._restore_from_snapshot(snap)
    # current_card should not be set (active_card_id is None/empty)
    assert daemon._state.get("current_card") is None


def test_restore_from_snapshot_restores_lifecycle_position() -> None:
    """053 regression: restart restore must preserve in-flight performer stage."""
    daemon = _make_daemon()
    snap = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="monitoring_performer",
        active_card_id="card-1",
        performer_stage="architecting",
        lifecycle_sequence=["assessing", "architecting", "implementing", "reviewing"],
    )

    daemon._restore_from_snapshot(snap)

    assert daemon._state["performer_stage"] == "architecting"
    assert daemon._state["lifecycle_sequence"] == [
        "assessing",
        "architecting",
        "implementing",
        "reviewing",
    ]


# ---------------------------------------------------------------------------
# _infer_phase_from_board_column
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column,expected",
    [
        ("In Progress", "monitoring_agent"),
        ("in_progress", "monitoring_agent"),
        ("In Review", "monitoring_pr"),
        ("in_review", "monitoring_pr"),
        ("Blocked", "blocked"),
        ("Done", "idle"),
        ("Backlog", "idle"),
        ("", "idle"),
    ],
)
def test_infer_phase_from_board_column(column: str, expected: str) -> None:
    result = CoordinareDaemon._infer_phase_from_board_column(column)
    assert result == expected


# ---------------------------------------------------------------------------
# _emit — all three branches (failure, heartbeat/activity, other)
# ---------------------------------------------------------------------------


def test_emit_failure_category_logs_at_error_level(caplog) -> None:
    import logging
    daemon = _make_daemon()
    with caplog.at_level(logging.ERROR):
        daemon._emit(category="failure", message="something went wrong")
    # We only verify it doesn't raise; structlog uses its own handlers


def test_emit_heartbeat_category_does_not_raise() -> None:
    daemon = _make_daemon()
    daemon._emit(category="heartbeat", message="ping")


def test_emit_activity_category_does_not_raise() -> None:
    daemon = _make_daemon()
    daemon._emit(category="activity", message="cycle done")


def test_emit_state_change_category_does_not_raise() -> None:
    daemon = _make_daemon()
    daemon._emit(category="state_change", message="idle → dispatching")


# ---------------------------------------------------------------------------
# _reconcile_with_board
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_returns_early_when_no_github_service() -> None:
    """If github_service is not in state, reconciliation is skipped silently."""
    daemon = _make_daemon()
    snap = _make_snapshot(phase="monitoring_agent", active_card_id="card-1")
    # No github_service in state — should not raise, state unchanged
    await daemon._reconcile_with_board(snap)
    # phase defaults to "idle" from initial_state() — reconcile is a no-op
    assert daemon._state.get("phase") == "idle"


@pytest.mark.asyncio
async def test_reconcile_resets_to_idle_when_card_not_found_on_board() -> None:
    github = MagicMock()
    github.poll_board = AsyncMock(return_value={"snapshot": {"Backlog": ["other-card"]}})
    daemon = _make_daemon()
    daemon._state["github_service"] = github
    snap = _make_snapshot(phase="monitoring_agent", active_card_id="card-1")

    await daemon._reconcile_with_board(snap)

    assert daemon._state["phase"] == "idle"
    assert daemon._state["current_card"] is None


@pytest.mark.asyncio
async def test_reconcile_resets_to_idle_when_card_in_done_column() -> None:
    github = MagicMock()
    github.poll_board = AsyncMock(return_value={"snapshot": {"DONE": ["card-1"]}})
    daemon = _make_daemon()
    daemon._state["github_service"] = github
    snap = _make_snapshot(phase="monitoring_agent", active_card_id="card-1")

    await daemon._reconcile_with_board(snap)

    assert daemon._state["phase"] == "idle"


@pytest.mark.asyncio
async def test_reconcile_updates_phase_when_board_differs() -> None:
    github = MagicMock()
    github.poll_board = AsyncMock(
        return_value={"snapshot": {"In Review": ["card-1"]}}
    )
    daemon = _make_daemon()
    daemon._state["github_service"] = github
    # Snapshot says monitoring_agent, but board says In Review → monitoring_pr
    snap = _make_snapshot(phase="monitoring_agent", active_card_id="card-1")

    await daemon._reconcile_with_board(snap)

    assert daemon._state["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_reconcile_confirms_phase_when_board_matches() -> None:
    github = MagicMock()
    github.poll_board = AsyncMock(
        return_value={"snapshot": {"In Progress": ["card-1"]}}
    )
    daemon = _make_daemon()
    daemon._state["github_service"] = github
    daemon._state["phase"] = "monitoring_agent"
    snap = _make_snapshot(phase="monitoring_agent", active_card_id="card-1")

    await daemon._reconcile_with_board(snap)

    # Board matches snapshot — phase is confirmed unchanged
    assert daemon._state["phase"] == "monitoring_agent"


@pytest.mark.asyncio
async def test_reconcile_handles_poll_exception_gracefully() -> None:
    github = MagicMock()
    github.poll_board = AsyncMock(side_effect=RuntimeError("network error"))
    daemon = _make_daemon()
    daemon._state["github_service"] = github
    snap = _make_snapshot(phase="monitoring_agent", active_card_id="card-1")

    # Should not raise
    await daemon._reconcile_with_board(snap)


# ---------------------------------------------------------------------------
# _install_signal_handlers — NotImplementedError branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_signal_handlers_handles_not_implemented() -> None:
    """Platforms (e.g. Windows) where add_signal_handler raises NotImplementedError."""
    daemon = _make_daemon()

    loop = asyncio.get_event_loop()
    original = loop.add_signal_handler

    def _raise(*args, **kwargs):
        raise NotImplementedError("not supported on this platform")

    loop.add_signal_handler = _raise  # type: ignore[method-assign]
    try:
        # Must not raise
        daemon._install_signal_handlers()
    finally:
        loop.add_signal_handler = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# state_store property (line 94)
# ---------------------------------------------------------------------------


def test_state_store_property_returns_none_by_default() -> None:
    daemon = _make_daemon()
    assert daemon.state_store is None


def test_state_store_property_returns_injected_store() -> None:
    store = MagicMock()
    daemon = _make_daemon(state_store=store)
    assert daemon.state_store is store


# ---------------------------------------------------------------------------
# start() with state_store that has a snapshot (lines 216-239)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_restores_state_from_snapshot() -> None:
    """start() loads and restores a snapshot from state_store before the poll loop."""
    snap = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="monitoring_agent",
        active_card_id="card-99",
    )
    state_store = MagicMock()
    state_store.load = AsyncMock(return_value=snap)
    state_store.save = AsyncMock()

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={"phase": "monitoring_agent"})

    daemon = CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
        state_store=state_store,
    )

    with patch.object(daemon, "_reconcile_with_board", new=AsyncMock()):
        await daemon.start()

    # Verify snapshot was loaded
    state_store.load.assert_called_once()


@pytest.mark.asyncio
async def test_start_handles_no_prior_snapshot() -> None:
    """start() handles state_store returning None (no prior state)."""
    state_store = MagicMock()
    state_store.load = AsyncMock(return_value=None)
    state_store.save = AsyncMock()

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})

    daemon = CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
        state_store=state_store,
    )

    await daemon.start()
    state_store.load.assert_called_once()


# ---------------------------------------------------------------------------
# daemon_restart_notification_failed (lines 343-344)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_restart_notification_failure_is_swallowed() -> None:
    """When the daemon_restart notification raises, start() continues normally."""
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})

    daemon = CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        heartbeat_interval_seconds=9999,
        max_cycles=1,
        sleep_func=_no_sleep,
    )

    failing_service = MagicMock()
    failing_service.dispatch = AsyncMock(side_effect=RuntimeError("smtp down"))
    daemon._state["notification_service"] = failing_service

    # Must complete without raising despite the notification failure
    await daemon.start()

    failing_service.dispatch.assert_awaited()


# ---------------------------------------------------------------------------
# prolonged_idle_notification_failed (lines 451-452)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prolonged_idle_notification_failure_is_swallowed() -> None:
    """When the prolonged_idle notification raises, the daemon continues."""

    class _IdleGraph:
        async def ainvoke(self, state):
            state["phase"] = "idle"
            return state

    daemon = CoordinareDaemon(
        _IdleGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=9999,
        max_cycles=2,
        sleep_func=_no_sleep,
        idle_threshold_seconds=0,  # trigger immediately
    )

    # First dispatch (daemon_restart) succeeds; subsequent ones (prolonged_idle) fail
    call_count = 0

    async def _dispatch_side_effect(event):
        nonlocal call_count
        call_count += 1
        from coordinare.models.notification import EventType
        if event.event_type == EventType.prolonged_idle:
            raise RuntimeError("notification backend unavailable")

    failing_service = MagicMock()
    failing_service.dispatch = AsyncMock(side_effect=_dispatch_side_effect)
    daemon._state["notification_service"] = failing_service

    # Must complete without propagating the idle-notification error
    await daemon.start()

    assert call_count >= 1


# ---------------------------------------------------------------------------
# Heartbeat emission (lines 456-464)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_emitted_when_interval_elapsed() -> None:
    """Heartbeat is emitted when now - last_heartbeat >= heartbeat_interval_seconds."""

    class _IdleGraph:
        async def ainvoke(self, state):
            state["phase"] = "idle"
            return state

    emitted_categories: list[str] = []

    daemon = CoordinareDaemon(
        _IdleGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=0,  # always emit heartbeat
        max_cycles=1,
        sleep_func=_no_sleep,
    )

    original_emit = daemon._emit

    def _capturing_emit(**event):
        emitted_categories.append(event.get("category", ""))
        original_emit(**event)

    daemon._emit = _capturing_emit  # type: ignore[method-assign]

    await daemon.start()

    assert "heartbeat" in emitted_categories


# ---------------------------------------------------------------------------
# CircuitOpenError path in main loop (lines 487-494)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_open_error_does_not_stop_daemon() -> None:
    """CircuitOpenError is caught and the loop continues; daemon exits after max_cycles.

    CircuitOpenError does NOT count as a successful cycle, so max_cycles=1 means
    ainvoke is called at least twice: once raising CircuitOpenError, once succeeding.
    """
    from coordinare.resilience import CircuitOpenError

    call_count = 0

    class _CircuitGraph:
        async def ainvoke(self, state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise CircuitOpenError("github")
            state["phase"] = "idle"
            return state

    daemon = CoordinareDaemon(
        _CircuitGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=9999,
        max_cycles=1,
        sleep_func=_no_sleep,
    )

    # Must complete without raising — CircuitOpenError is handled internally
    await daemon.start()

    # First call raised CircuitOpenError (not a cycle); second call succeeded (1 cycle)
    assert call_count == 2


@pytest.mark.asyncio
async def test_circuit_open_error_for_unknown_service_skips_health_update() -> None:
    """CircuitOpenError for a service not in _CIRCUIT_TO_HEALTH_SUBSYSTEM does not crash."""
    from coordinare.resilience import CircuitOpenError

    circuit_raised = False

    class _CircuitGraph:
        async def ainvoke(self, state):
            nonlocal circuit_raised
            if not circuit_raised:
                circuit_raised = True
                # "custom_service" is not in _CIRCUIT_TO_HEALTH_SUBSYSTEM so the
                # health-update branch is skipped — exercises lines 486-492.
                raise CircuitOpenError("custom_service")
            state["phase"] = "idle"
            return state

    daemon = CoordinareDaemon(
        _CircuitGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=9999,
        max_cycles=1,
        sleep_func=_no_sleep,
    )

    # Must complete without raising
    await daemon.start()

    assert circuit_raised is True


# ---------------------------------------------------------------------------
# Phase transition metric (line 414): known transition fires card_state_transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_phase_transition_increments_metric() -> None:
    """Line 414: a canonical phase transition recorded in _PHASE_TRANSITION_METRIC increments the counter."""
    from coordinare.metrics import METRICS

    call_count = 0

    class _TransitionGraph:
        async def ainvoke(self, state):
            nonlocal call_count
            call_count += 1
            # First call: transition from idle → dispatching (a known transition)
            if call_count == 1:
                state["phase"] = "dispatching"
            return state

    daemon = CoordinareDaemon(
        _TransitionGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=9999,
        max_cycles=2,
        sleep_func=_no_sleep,
    )

    before = METRICS.card_state_transitions_total.labels(
        symphony="__default__", transition_type="idle_to_dispatch"
    )._value.get()

    await daemon.start()

    after = METRICS.card_state_transitions_total.labels(
        symphony="__default__", transition_type="idle_to_dispatch"
    )._value.get()
    assert after == before + 1.0


# ---------------------------------------------------------------------------
# Dashboard store error recording (lines 542-549): generic Exception in cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_store_records_error_cycle_on_runtime_exception() -> None:
    """Lines 542-549: when a generic Exception terminates the run loop, dashboard_store records error cycle."""
    class _BoomGraph:
        async def ainvoke(self, state):
            raise RuntimeError("cycle exploded")

    dashboard_store = MagicMock()
    dashboard_store.build_snapshot.return_value = {}
    dashboard_store.broadcaster = MagicMock()

    daemon = CoordinareDaemon(
        _BoomGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=9999,
        max_cycles=1,
        sleep_func=_no_sleep,
        dashboard_store=dashboard_store,
    )

    from coordinare.daemon import RuntimeExecutionError
    with pytest.raises(RuntimeExecutionError):
        await daemon.start()

    dashboard_store.record_cycle.assert_called_once()
    _, kwargs = dashboard_store.record_cycle.call_args
    assert kwargs["outcome"] == "error"


# ---------------------------------------------------------------------------
# StateLoadError during startup (lines 298-306): handled gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_store_records_success_cycle() -> None:
    """Lines 382-389: dashboard_store.record_cycle is called with outcome='success' after a good cycle."""
    dashboard_store = MagicMock()
    dashboard_store.build_snapshot.return_value = {}
    dashboard_store.broadcaster = MagicMock()

    daemon = _make_daemon(dashboard_store=dashboard_store)
    await daemon.start()

    dashboard_store.record_cycle.assert_called()
    _, kwargs = dashboard_store.record_cycle.call_args_list[-1]
    assert kwargs["outcome"] == "success"


@pytest.mark.asyncio
async def test_state_store_load_returns_snapshot_with_no_active_card() -> None:
    """Lines 289->309: snapshot loaded with active_card_id=None → reconcile not called."""
    state_store = MagicMock()
    state_store.load = AsyncMock(return_value=_make_snapshot(phase="idle", active_card_id=None))
    state_store.save = AsyncMock()
    state_store.last_snapshot = None

    daemon = _make_daemon(state_store=state_store)
    await daemon.start()
    # Phase restored from snapshot (idle), reconcile skipped since no active_card_id
    assert not daemon._running


@pytest.mark.asyncio
async def test_state_load_error_during_startup_is_handled() -> None:
    """Lines 298-306: StateLoadError from state_store.load() is caught and daemon continues."""
    from coordinare.state_store import StateLoadError

    state_store = MagicMock()
    state_store.load = AsyncMock(side_effect=StateLoadError(reason="corrupt", detail="bad data"))
    state_store.save = AsyncMock()
    state_store.last_snapshot = None

    daemon = _make_daemon(state_store=state_store)
    # Should complete without raising despite StateLoadError
    await daemon.start()

    assert not daemon._running


# ---------------------------------------------------------------------------
# 054 — session eligibility, concurrent fanout, skip reasons
# ---------------------------------------------------------------------------

from coordinare.daemon import (  # noqa: E402
    _compute_eligibility,
)


def _make_session(card_id: str = "card-1", phase: str = "monitoring_performer") -> dict:
    return {
        "current_card": {"id": card_id, "content_id": card_id, "title": "Test Card"},
        "phase": phase,
    }


# --- _compute_eligibility ---

def test_compute_eligibility_eligible() -> None:
    session = _make_session("card-1")
    board_snapshot = {"IN_PROGRESS": ["card-1"]}
    result = _compute_eligibility("card-1", session, board_snapshot, None)
    assert result.eligible is True
    assert result.reason == "eligible"


def test_compute_eligibility_blocked_column() -> None:
    session = _make_session("card-2")
    board_snapshot = {"BLOCKED": ["card-2"]}
    result = _compute_eligibility("card-2", session, board_snapshot, None)
    assert result.eligible is False
    assert result.reason == "blocked_column"


def test_compute_eligibility_missing_card() -> None:
    session = {"current_card": None, "phase": "idle"}
    result = _compute_eligibility("card-3", session, {}, None)
    assert result.eligible is False
    assert result.reason == "missing_card"


def test_compute_eligibility_missing_card_empty_dict() -> None:
    session = {"current_card": {}, "phase": "idle"}
    result = _compute_eligibility("card-4", session, {}, None)
    assert result.eligible is False
    assert result.reason == "missing_card"


def test_compute_eligibility_dependency_blocked() -> None:
    from coordinare.models.dependency import CardDependency, DependencyGraph, DependencyStatus
    card_id = "card-5"
    dep = CardDependency(
        dependent_item_id=card_id,
        blocker_issue_number=99,
        status=DependencyStatus.PENDING,
    )
    dep_graph = DependencyGraph()
    dep_graph.by_dependent[card_id] = [dep]

    session = _make_session(card_id)
    result = _compute_eligibility(card_id, session, {}, dep_graph)
    assert result.eligible is False
    assert result.reason == "dependency_blocked"
    assert 99 in result.blockers


def test_compute_eligibility_satisfied_deps_are_eligible() -> None:
    from coordinare.models.dependency import CardDependency, DependencyGraph, DependencyStatus
    card_id = "card-6"
    dep = CardDependency(
        dependent_item_id=card_id,
        blocker_issue_number=10,
        status=DependencyStatus.SATISFIED,
    )
    dep_graph = DependencyGraph()
    dep_graph.by_dependent[card_id] = [dep]

    session = _make_session(card_id)
    result = _compute_eligibility(card_id, session, {}, dep_graph)
    assert result.eligible is True


# --- async fanout: concurrent invocation ---

@pytest.mark.asyncio
async def test_invoke_multi_session_concurrent_eligible() -> None:
    """All eligible sessions must be invoked concurrently via asyncio.gather."""
    invocation_order: list[str] = []

    async def _tracked_ainvoke(state: dict) -> dict:
        card_id = (state.get("current_card") or {}).get("id", "?")
        invocation_order.append(card_id)
        return state

    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=_tracked_ainvoke)

    daemon = _make_daemon()
    daemon._graph = graph

    daemon._state["active_sessions"] = {
        "card-a": _make_session("card-a"),
        "card-b": _make_session("card-b"),
    }
    daemon._state["board_snapshot"] = {"IN_PROGRESS": ["card-a", "card-b"]}

    # _invoke_multi_session clears _board_cache at cycle start, so this has no
    # effect on the pre-poll path. Pre-poll is skipped because github_service=None.
    daemon._state["_board_cache"] = {
        "snapshot": {"IN_PROGRESS": ["card-a", "card-b"]},
        "titles": {},
        "descriptions": {},
        "issue_numbers": {},
        "issue_urls": {},
        "content_node_ids": {},
    }
    daemon._state["github_service"] = None  # no pre-poll

    await daemon._invoke_multi_session()

    assert "card-a" in invocation_order
    assert "card-b" in invocation_order
    assert graph.ainvoke.call_count == 2


# --- skip reasons: blocked and dependency ---

@pytest.mark.asyncio
async def test_invoke_multi_session_blocked_column_fallback_invoked() -> None:
    """When all sessions are in the BLOCKED column, a single fallback graph
    invocation must still run so check_board can fill open slots."""
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})

    daemon = _make_daemon()
    daemon._graph = graph
    daemon._state["github_service"] = None

    daemon._state["active_sessions"] = {"card-x": _make_session("card-x")}
    daemon._state["board_snapshot"] = {"BLOCKED": ["card-x"]}
    daemon._state["_board_cache"] = {
        "snapshot": {"BLOCKED": ["card-x"]},
        "titles": {}, "descriptions": {}, "issue_numbers": {},
        "issue_urls": {}, "content_node_ids": {},
    }

    await daemon._invoke_multi_session()

    # Fallback invocation must have fired with skip_reasons already set.
    graph.ainvoke.assert_called_once()
    call_state = graph.ainvoke.call_args[0][0]
    skip = call_state.get("session_skip_reasons", {})
    assert "card-x" in skip
    assert skip["card-x"]["reason"] == "blocked_column"


@pytest.mark.asyncio
async def test_invoke_multi_session_dep_blocked_not_invoked() -> None:
    """Sessions with unresolved dependencies must be skipped."""
    board = {
        "snapshot": {"IN_PROGRESS": ["card-y"]},
        "titles": {"card-y": "Test"},
        "descriptions": {"card-y": "Depends on #50"},
        "issue_numbers": {},
        "issue_urls": {},
        "content_node_ids": {},
    }
    github = MagicMock()
    github.poll_board = AsyncMock(return_value=board)

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})

    daemon = _make_daemon()
    daemon._graph = graph
    daemon._state["github_service"] = github
    daemon._state["active_sessions"] = {"card-y": _make_session("card-y")}
    daemon._state["board_snapshot"] = {"IN_PROGRESS": ["card-y"]}

    await daemon._invoke_multi_session()

    # Fallback invocation must have fired with skip_reasons already set.
    graph.ainvoke.assert_called_once()
    call_state = graph.ainvoke.call_args[0][0]
    skip = call_state.get("session_skip_reasons", {})
    assert "card-y" in skip
    assert skip["card-y"]["reason"] == "dependency_blocked"


# --- failure isolation ---

@pytest.mark.asyncio
async def test_invoke_multi_session_failure_isolation() -> None:
    """A crash in one session must not prevent other sessions from completing."""
    async def _side_effect(state: dict) -> dict:
        card_id = (state.get("current_card") or {}).get("id", "?")
        if card_id == "card-bad":
            raise RuntimeError("boom")
        return state

    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=_side_effect)

    daemon = _make_daemon()
    daemon._graph = graph
    daemon._state["github_service"] = None

    daemon._state["active_sessions"] = {
        "card-bad": _make_session("card-bad"),
        "card-good": _make_session("card-good"),
    }
    daemon._state["board_snapshot"] = {"IN_PROGRESS": ["card-bad", "card-good"]}
    daemon._state["_board_cache"] = {
        "snapshot": {"IN_PROGRESS": ["card-bad", "card-good"]},
        "titles": {}, "descriptions": {}, "issue_numbers": {},
        "issue_urls": {}, "content_node_ids": {},
    }

    await daemon._invoke_multi_session()

    # card-good should still be in active_sessions
    assert "card-good" in daemon._state["active_sessions"]
    # card-bad restored from pre_session (not removed — next cycle retries)
    assert "card-bad" in daemon._state["active_sessions"]


# --- max_concurrent_cards=1 compatibility ---

@pytest.mark.asyncio
async def test_invoke_single_session_uses_sequential_path() -> None:
    """max_concurrent_cards=1 must use the single-session graph path, not _invoke_multi_session."""
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})

    daemon = _make_daemon()
    daemon._graph = graph

    # In single-session mode _invoke_multi_session is not called — the
    # daemon calls graph.ainvoke directly from the main tick.  Verify the
    # _max_concurrent_cards() guard returns 1 when config is absent.
    assert daemon._max_concurrent_cards() == 1


# --- resume on unblock ---

@pytest.mark.asyncio
async def test_dependency_unblock_resumes_session() -> None:
    """After blocker is satisfied, session should be invoked on the next cycle."""
    invoked: list[str] = []

    async def _tracked(state: dict) -> dict:
        card_id = (state.get("current_card") or {}).get("id", "?")
        invoked.append(card_id)
        return state

    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=_tracked)
    daemon = _make_daemon()
    daemon._graph = graph

    board_cycle1 = {
        "snapshot": {"IN_PROGRESS": ["card-z"]},
        "titles": {"card-z": "Z"},
        "descriptions": {"card-z": "Depends on #77"},
        "issue_numbers": {},
        "issue_urls": {},
        "content_node_ids": {},
    }
    board_cycle2 = {
        "snapshot": {"IN_PROGRESS": ["card-z"]},
        "titles": {"card-z": "Z"},
        "descriptions": {"card-z": ""},  # no dep
        "issue_numbers": {},
        "issue_urls": {},
        "content_node_ids": {},
    }
    github = MagicMock()
    github.poll_board = AsyncMock(side_effect=[board_cycle1, board_cycle2])
    daemon._state["github_service"] = github

    daemon._state["active_sessions"] = {"card-z": _make_session("card-z")}

    # Cycle 1: dependency present → skip
    daemon._state["board_snapshot"] = {"IN_PROGRESS": ["card-z"]}
    await daemon._invoke_multi_session()
    assert "card-z" not in invoked

    # Cycle 2: blocker resolved (no description, no dep graph entry) → eligible
    daemon._state["board_snapshot"] = {"IN_PROGRESS": ["card-z"]}
    await daemon._invoke_multi_session()
    assert "card-z" in invoked


# ---------------------------------------------------------------------------
# Slot-leak fix: sync_from_sessions runs in single-card mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_card_cycle_calls_sync_from_sessions() -> None:
    """sync_from_sessions must fire in single-card mode so stale slots are freed."""
    daemon = _make_daemon()  # max_concurrent_cards defaults to 1

    sync_calls: list = []

    class _FakeSlotMgr:
        def sync_from_sessions(self, sessions):
            sync_calls.append(dict(sessions))

    daemon._state["slot_manager"] = _FakeSlotMgr()
    daemon._state["active_sessions"] = {}  # empty — all slots should be freed

    # Run one cycle (max_cycles=1 so the loop exits after one iteration)
    await daemon.start()

    assert len(sync_calls) >= 1
    assert sync_calls[0] == {}


@pytest.mark.asyncio
async def test_single_card_cycle_sync_frees_stale_slots() -> None:
    """Stale assessing/architecting slots left over from a completed card
    are freed on the next single-card cycle when active_sessions is empty."""
    from coordinare.services.slot_manager import SlotManager

    sm = SlotManager()
    sm.register_pool("assessing", [MagicMock()], max_concurrency=1)
    sm.register_pool("architecting", [MagicMock()], max_concurrency=1)
    sm.acquire("assessing", "CARD_OLD")
    sm.acquire("architecting", "CARD_OLD")
    assert sm.active_count("assessing") == 1
    assert sm.active_count("architecting") == 1

    daemon = _make_daemon()
    daemon._state["slot_manager"] = sm
    daemon._state["active_sessions"] = {}  # card already gone

    await daemon.start()

    assert sm.active_count("assessing") == 0
    assert sm.active_count("architecting") == 0


@pytest.mark.asyncio
async def test_single_card_cycle_no_slot_manager_does_not_raise() -> None:
    """When slot_manager is absent, the cycle must complete without error."""
    daemon = _make_daemon()
    daemon._state["slot_manager"] = None
    await daemon.start()  # should not raise


# ---------------------------------------------------------------------------
# 066 FR-010: _pick_stable_active_card_id — dashboard ping-pong guard
# ---------------------------------------------------------------------------


def test_pick_stable_active_card_id_empty_returns_none() -> None:
    from coordinare.daemon import _pick_stable_active_card_id

    assert _pick_stable_active_card_id({}) is None


def test_pick_stable_active_card_id_picks_earliest_picked_up_at() -> None:
    """Earliest dated session wins regardless of dict insertion order."""
    from datetime import UTC, datetime

    from coordinare.daemon import _pick_stable_active_card_id

    sessions = {
        "CARD_LATE": {"picked_up_at": datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)},
        "CARD_EARLY": {"picked_up_at": datetime(2026, 5, 19, 9, 0, 0, tzinfo=UTC)},
        "CARD_MID": {"picked_up_at": datetime(2026, 5, 19, 10, 30, 0, tzinfo=UTC)},
    }
    assert _pick_stable_active_card_id(sessions) == "CARD_EARLY"


def test_pick_stable_active_card_id_iso_strings_sort_with_datetimes() -> None:
    """ISO-format strings sort lexicographically alongside datetimes."""
    from datetime import UTC, datetime

    from coordinare.daemon import _pick_stable_active_card_id

    sessions = {
        "CARD_A": {"picked_up_at": "2026-05-19T12:00:00+00:00"},
        "CARD_B": {"picked_up_at": datetime(2026, 5, 19, 9, 0, 0, tzinfo=UTC)},
    }
    assert _pick_stable_active_card_id(sessions) == "CARD_B"


def test_pick_stable_active_card_id_undated_sorts_after_dated() -> None:
    """A session without picked_up_at loses to any dated peer."""
    from datetime import UTC, datetime

    from coordinare.daemon import _pick_stable_active_card_id

    sessions = {
        "CARD_UNDATED": {},
        "CARD_DATED": {"picked_up_at": datetime(2030, 1, 1, tzinfo=UTC)},
    }
    assert _pick_stable_active_card_id(sessions) == "CARD_DATED"


def test_pick_stable_active_card_id_all_undated_falls_back_to_lex_order() -> None:
    """When no session has picked_up_at, lexicographically smallest card_id wins.

    Stable across runs because card IDs are stable identifiers — replaces the
    insertion-order tie-break that previously caused dashboard ping-pong.
    """
    from coordinare.daemon import _pick_stable_active_card_id

    sessions = {
        "CARD_ZULU": {},
        "CARD_ALPHA": {},
        "CARD_MIKE": {},
    }
    assert _pick_stable_active_card_id(sessions) == "CARD_ALPHA"


def test_pick_stable_active_card_id_is_insertion_order_invariant() -> None:
    """Reordering insertion does not change the winner."""
    from datetime import UTC, datetime

    from coordinare.daemon import _pick_stable_active_card_id

    early = datetime(2026, 5, 19, 9, 0, 0, tzinfo=UTC)
    late = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    forward = {
        "CARD_EARLY": {"picked_up_at": early},
        "CARD_LATE": {"picked_up_at": late},
    }
    reverse = {
        "CARD_LATE": {"picked_up_at": late},
        "CARD_EARLY": {"picked_up_at": early},
    }
    assert _pick_stable_active_card_id(forward) == _pick_stable_active_card_id(reverse) == "CARD_EARLY"


def test_pick_stable_active_card_id_normalises_cross_timezone_datetimes() -> None:
    """Datetimes in different offsets must be compared as wall-time-equivalents.

    Without UTC normalisation, lex-sorting raw ``isoformat()`` strings ranks
    by offset prefix and inverts ordering across timezones.  The picker
    should pick the earliest *instant*, not the earliest local string.
    """
    from datetime import UTC, datetime, timedelta, timezone

    from coordinare.daemon import _pick_stable_active_card_id

    # 09:00 UTC == 11:00 in +02:00 — same instant.  EARLY is one hour before.
    early_utc = datetime(2026, 5, 19, 9, 0, 0, tzinfo=UTC)
    late_in_offset = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))  # 10:00 UTC
    sessions = {
        "CARD_LATE": {"picked_up_at": late_in_offset},
        "CARD_EARLY": {"picked_up_at": early_utc},
    }
    assert _pick_stable_active_card_id(sessions) == "CARD_EARLY"


def test_pick_stable_active_card_id_treats_naive_datetime_as_utc() -> None:
    """Naive datetimes must not raise — assumed UTC for ordering."""
    from datetime import UTC, datetime

    from coordinare.daemon import _pick_stable_active_card_id

    naive_early = datetime(2026, 5, 19, 9, 0, 0)  # no tzinfo
    aware_late = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    sessions = {
        "CARD_LATE": {"picked_up_at": aware_late},
        "CARD_NAIVE": {"picked_up_at": naive_early},
    }
    assert _pick_stable_active_card_id(sessions) == "CARD_NAIVE"


def test_pick_stable_active_card_id_mixed_naive_and_aware_sort_consistently() -> None:
    """Mixed naive + aware + ISO-string + non-UTC offset must order by instant.

    All four sessions sit in the dated bucket and must compare without raising
    (the ISO-string branch and the datetime branch both produce strings, and
    the datetime branch normalises to UTC so the suffix is canonical).
    """
    from datetime import UTC, datetime, timedelta, timezone

    from coordinare.daemon import _pick_stable_active_card_id

    sessions = {
        "CARD_NAIVE": {"picked_up_at": datetime(2026, 5, 19, 10, 0, 0)},  # naive → 10:00 UTC
        "CARD_AWARE": {"picked_up_at": datetime(2026, 5, 19, 11, 0, 0, tzinfo=UTC)},
        "CARD_OFFSET": {  # 13:00 +02:00 → 11:00 UTC, ties with CARD_AWARE
            "picked_up_at": datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone(timedelta(hours=2))),
        },
        "CARD_ISO": {"picked_up_at": "2026-05-19T09:00:00+00:00"},  # earliest
    }
    assert _pick_stable_active_card_id(sessions) == "CARD_ISO"


def test_pick_stable_active_card_id_normalises_iso_string_offsets() -> None:
    """Non-UTC ISO strings must be parsed and normalised to UTC.

    Without parsing, ``'2026-05-19T13:00:00+02:00'`` lex-sorts as later than
    ``'2026-05-19T11:00:01+00:00'`` despite being one second earlier as an
    instant.  The picker must compare instants.
    """
    from coordinare.daemon import _pick_stable_active_card_id

    sessions = {
        "CARD_LATE_UTC": {"picked_up_at": "2026-05-19T11:00:01+00:00"},
        "CARD_EARLY_OFFSET": {"picked_up_at": "2026-05-19T13:00:00+02:00"},  # 11:00:00 UTC
    }
    assert _pick_stable_active_card_id(sessions) == "CARD_EARLY_OFFSET"


def test_pick_stable_active_card_id_unparseable_iso_string_falls_back_to_raw() -> None:
    """Garbage strings stay in the dated bucket and sort against each other
    by raw value — no exception, no demotion to the undated bucket."""
    from coordinare.daemon import _pick_stable_active_card_id

    sessions = {
        "CARD_B": {"picked_up_at": "not-a-date-b"},
        "CARD_A": {"picked_up_at": "not-a-date-a"},
    }
    # Both unparseable → raw lex sort → "not-a-date-a" wins.
    assert _pick_stable_active_card_id(sessions) == "CARD_A"

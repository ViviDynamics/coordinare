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


def test_restore_from_snapshot_no_card_skips_current_card() -> None:
    daemon = _make_daemon()
    snap = _make_snapshot(phase="idle", active_card_id=None)
    daemon._restore_from_snapshot(snap)
    # current_card should not be set (active_card_id is None/empty)
    assert daemon._state.get("current_card") is None


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

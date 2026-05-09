from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.metrics import CoordinareMetrics
from coordinare.state_store import StateStore, WorkflowSnapshot

if TYPE_CHECKING:
    from pathlib import Path


# --- Mock helpers ---


def _make_snapshot(**overrides) -> WorkflowSnapshot:
    defaults = {
        "snapshot_at": datetime.now(UTC),
        "phase": "idle",
    }
    defaults.update(overrides)
    return WorkflowSnapshot(**defaults)


class _Graph:
    """Minimal graph stub that returns state unchanged (no-op cycle)."""

    def __init__(self, phase: str = "idle"):
        self._phase = phase

    async def ainvoke(self, state):
        state["phase"] = self._phase
        return state


class _GitHub:
    """Mock github service returning a configurable board snapshot."""

    project_id: int = 1  # non-zero so _reconcile_with_board treats it as initialized

    def __init__(self, board_snapshot: dict | None = None):
        self._board_snapshot = board_snapshot or {}

    async def poll_board(self):
        return {"snapshot": self._board_snapshot}


async def _no_sleep(_: int) -> None:
    return None


# --- T013/T023: Crash Recovery Integration Tests ---


@pytest.mark.asyncio
async def test_daemon_restores_phase_and_card_from_snapshot(tmp_path: Path) -> None:
    """After a crash, daemon restores monitoring_agent phase and card from persisted state."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    # Simulate prior session: save a snapshot with active card in monitoring_agent
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_CRASH",
        active_card_title="Crash Test Card",
        active_card_column="In Progress",
        agent_session_id="sess_crash",
    )
    await store.save(snapshot)

    # Restart daemon with persisted state — board confirms card is in IN_PROGRESS
    github = _GitHub(board_snapshot={"IN_PROGRESS": ["PVT_CRASH"]})
    graph = _Graph(phase="monitoring_agent")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)
    daemon.state["github_service"] = github

    await daemon.start()

    # Assert daemon restored to correct phase and card
    assert daemon.state.get("phase") == "monitoring_agent"
    card = daemon.state.get("current_card")
    assert isinstance(card, dict)
    assert card["id"] == "PVT_CRASH"
    assert card["title"] == "Crash Test Card"
    dispatch = daemon.state.get("agent_dispatch")
    assert isinstance(dispatch, dict)
    assert dispatch["session_id"] == "sess_crash"


@pytest.mark.asyncio
async def test_daemon_starts_fresh_when_snapshot_is_idle(tmp_path: Path) -> None:
    """A persisted idle snapshot results in a clean fresh start (acceptance scenario 4)."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    snapshot = _make_snapshot(phase="idle")
    await store.save(snapshot)

    graph = _Graph(phase="idle")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)

    await daemon.start()

    assert daemon.state.get("phase") == "idle"
    assert daemon.state.get("current_card") is None


@pytest.mark.asyncio
async def test_daemon_starts_fresh_when_no_state_file(tmp_path: Path) -> None:
    """With no state file, daemon starts fresh in idle."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "missing.json", metrics=metrics)

    graph = _Graph(phase="idle")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)

    await daemon.start()

    assert daemon.state.get("phase") == "idle"


@pytest.mark.asyncio
async def test_daemon_starts_fresh_on_corrupt_state(tmp_path: Path) -> None:
    """Corrupted state file → logs warning, starts fresh in idle, no crash."""
    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    path.write_text("not valid json {{{")
    store = StateStore(path=path, metrics=metrics)

    graph = _Graph(phase="idle")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)

    await daemon.start()

    assert daemon.state.get("phase") == "idle"
    assert daemon.state.get("current_card") is None


@pytest.mark.asyncio
async def test_board_reconciliation_card_missing_resets_to_idle(tmp_path: Path) -> None:
    """If persisted card is not on the board (e.g. moved to Done), daemon resets to idle."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_GONE",
        active_card_title="Gone Card",
        active_card_column="In Progress",
    )
    await store.save(snapshot)

    # Board has no trace of PVT_GONE
    github = _GitHub(board_snapshot={"IN_PROGRESS": [], "TODO": []})
    graph = _Graph(phase="idle")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)
    daemon.state["github_service"] = github

    await daemon.start()

    assert daemon.state.get("phase") == "idle"
    assert daemon.state.get("current_card") is None


@pytest.mark.asyncio
async def test_board_reconciliation_card_in_done_resets_to_idle(tmp_path: Path) -> None:
    """If persisted card is in Done column, daemon resets to idle."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_DONE",
        active_card_title="Done Card",
        active_card_column="In Progress",
    )
    await store.save(snapshot)

    github = _GitHub(board_snapshot={"DONE": ["PVT_DONE"], "IN_PROGRESS": []})
    graph = _Graph(phase="idle")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)
    daemon.state["github_service"] = github

    await daemon.start()

    assert daemon.state.get("phase") == "idle"
    assert daemon.state.get("current_card") is None


@pytest.mark.asyncio
async def test_board_reconciliation_column_advanced(tmp_path: Path) -> None:
    """Card moved from In Progress to In Review during downtime → phase advances."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_ADV",
        active_card_title="Advanced Card",
        active_card_column="In Progress",
    )
    await store.save(snapshot)

    # Board shows card moved to IN_REVIEW while we were down
    github = _GitHub(board_snapshot={"IN_REVIEW": ["PVT_ADV"], "IN_PROGRESS": []})
    graph = _Graph(phase="monitoring_pr")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)
    daemon.state["github_service"] = github

    await daemon.start()

    # Should have advanced to monitoring_pr
    assert daemon.state.get("phase") == "monitoring_pr"
    card = daemon.state.get("current_card")
    assert isinstance(card, dict)
    assert card["id"] == "PVT_ADV"


@pytest.mark.asyncio
async def test_board_reconciliation_column_matches(tmp_path: Path) -> None:
    """Card still in same column → restored state is confirmed."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_SAME",
        active_card_title="Same Card",
        active_card_column="In Progress",
    )
    await store.save(snapshot)

    github = _GitHub(board_snapshot={"IN_PROGRESS": ["PVT_SAME"]})
    graph = _Graph(phase="monitoring_agent")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)
    daemon.state["github_service"] = github

    await daemon.start()

    assert daemon.state.get("phase") == "monitoring_agent"
    card = daemon.state.get("current_card")
    assert isinstance(card, dict)
    assert card["id"] == "PVT_SAME"


@pytest.mark.asyncio
async def test_phase_transition_writes_snapshot(tmp_path: Path) -> None:
    """Phase transition during poll loop triggers snapshot save."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    # Graph transitions from idle to monitoring_agent
    graph = _Graph(phase="monitoring_agent")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)

    await daemon.start()

    # Snapshot should have been written with the new phase
    loaded = await store.load()
    assert loaded is not None
    assert loaded.phase == "monitoring_agent"


@pytest.mark.asyncio
async def test_no_snapshot_write_when_phase_unchanged(tmp_path: Path) -> None:
    """No phase transition → no snapshot written."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    # Graph keeps idle phase (matches initial)
    graph = _Graph(phase="idle")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)

    await daemon.start()

    # No snapshot should have been written (no phase change)
    loaded = await store.load()
    assert loaded is None


@pytest.mark.asyncio
async def test_board_reconciliation_blocked_column(tmp_path: Path) -> None:
    """Card in BLOCKED column during downtime → phase inferred as blocked."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_BLK",
        active_card_title="Blocked Card",
        active_card_column="In Progress",
    )
    await store.save(snapshot)

    github = _GitHub(board_snapshot={"BLOCKED": ["PVT_BLK"], "IN_PROGRESS": []})
    graph = _Graph(phase="blocked")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)
    daemon.state["github_service"] = github

    await daemon.start()

    assert daemon.state.get("phase") == "blocked"


@pytest.mark.asyncio
async def test_board_reconciliation_skipped_when_no_github(tmp_path: Path) -> None:
    """Reconciliation is skipped gracefully when no github_service is set."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_NOGIT",
        active_card_title="No GitHub Card",
        active_card_column="In Progress",
    )
    await store.save(snapshot)

    graph = _Graph(phase="monitoring_agent")
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=_no_sleep, state_store=store)
    # Intentionally NOT setting github_service

    await daemon.start()

    # State should still be restored from snapshot, just no reconciliation
    assert daemon.state.get("phase") == "monitoring_agent"

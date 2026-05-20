"""T045: Quickstart scenario walkthrough.

Exercises the five scenarios documented in specs/003-state-persistence/quickstart.md:
  1. Normal operation — phase transition writes snapshot to disk
  2. Crash recovery — restart resumes from persisted state
  3. Corrupted file — warning logged, fresh idle, no crash
  4. Permission error — verify_writable raises OSError
  5. Disk-full — save logs warning, prior state intact, no crash
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.metrics import CoordinareMetrics
from coordinare.state_store import CURRENT_SCHEMA_VERSION, StateStore, WorkflowSnapshot

if TYPE_CHECKING:
    from pathlib import Path


def _make_snapshot(**overrides) -> WorkflowSnapshot:
    defaults = {"snapshot_at": datetime.now(UTC), "phase": "idle"}
    defaults.update(overrides)
    return WorkflowSnapshot(**defaults)


class _TransitionGraph:
    """Graph that transitions from idle to monitoring_agent on first invoke."""

    def __init__(self) -> None:
        self._called = False

    async def ainvoke(self, state):
        if not self._called:
            self._called = True
            state["phase"] = "monitoring_agent"
        return state


class _IdleGraph:
    async def ainvoke(self, state):
        state["phase"] = "idle"
        return state


class _GitHub:
    def __init__(self, board_snapshot: dict | None = None):
        self._board_snapshot = board_snapshot or {}

    async def poll_board(self):
        return {"snapshot": self._board_snapshot}


async def _no_sleep(_: int) -> None:
    return None


# --- Scenario 1: Normal operation — phase transition writes snapshot ---


@pytest.mark.asyncio
async def test_scenario_normal_operation(tmp_path: Path) -> None:
    """After a phase transition, coordinare.state.json exists with correct phase."""
    path = tmp_path / "coordinare.state.json"
    metrics = CoordinareMetrics()
    store = StateStore(path=path, metrics=metrics)

    daemon = CoordinareDaemon(
        _TransitionGraph(), max_cycles=2, sleep_func=_no_sleep, state_store=store
    )
    await daemon.start()

    # State file should exist with the transitioned phase
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["phase"] == "monitoring_agent"
    assert data["schema_version"] == CURRENT_SCHEMA_VERSION
    assert "snapshot_at" in data


# --- Scenario 2: Crash recovery — restart resumes from persisted state ---


@pytest.mark.asyncio
async def test_scenario_crash_recovery(tmp_path: Path) -> None:
    """Simulated crash recovery: save state, create new daemon, verify it resumes."""
    path = tmp_path / "coordinare.state.json"
    metrics = CoordinareMetrics()
    store = StateStore(path=path, metrics=metrics)

    # Session 1: write state as if we were monitoring a card
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_QS",
        active_card_title="Quickstart Card",
        active_card_column="In Progress",
        agent_session_id="sess_qs",
    )
    await store.save(snapshot)

    # Session 2: new daemon reads persisted state (simulates restart after crash)
    store2 = StateStore(path=path, metrics=CoordinareMetrics())
    github = _GitHub(board_snapshot={"IN_PROGRESS": ["PVT_QS"]})

    class _KeepPhaseGraph:
        async def ainvoke(self, state):
            return state

    daemon = CoordinareDaemon(
        _KeepPhaseGraph(), max_cycles=1, sleep_func=_no_sleep, state_store=store2
    )
    daemon.state["github_service"] = github
    await daemon.start()

    assert daemon.state.get("phase") == "monitoring_agent"
    card = daemon.state.get("current_card")
    assert isinstance(card, dict)
    assert card["id"] == "PVT_QS"


# --- Scenario 3: Corrupted file — warning, fresh idle, no crash ---


@pytest.mark.asyncio
async def test_scenario_corrupted_file(tmp_path: Path) -> None:
    """echo 'not valid json' > coordinare.state.json → starts fresh, no crash."""
    path = tmp_path / "coordinare.state.json"
    path.write_text("not valid json")

    metrics = CoordinareMetrics()
    store = StateStore(path=path, metrics=metrics)
    daemon = CoordinareDaemon(
        _IdleGraph(), max_cycles=1, sleep_func=_no_sleep, state_store=store
    )

    await daemon.start()  # must not raise

    assert daemon.state.get("phase") == "idle"
    assert daemon.state.get("current_card") is None


# --- Scenario 4: Permission error — verify_writable raises OSError ---


def test_scenario_permission_error(tmp_path: Path) -> None:
    """Read-only directory → verify_writable raises OSError."""
    read_only = tmp_path / "readonly"
    read_only.mkdir()
    os.chmod(read_only, 0o444)

    metrics = CoordinareMetrics()
    store = StateStore(path=read_only / "state.json", metrics=metrics)

    try:
        with pytest.raises(OSError):
            store.verify_writable()
    finally:
        os.chmod(read_only, 0o755)


# --- Scenario 5: Disk-full — prior state survives, no crash ---


@pytest.mark.asyncio
async def test_scenario_disk_full(tmp_path: Path) -> None:
    """Disk-full during save → prior state file intact, no crash, metric incremented."""
    path = tmp_path / "coordinare.state.json"
    metrics = CoordinareMetrics()
    store = StateStore(path=path, metrics=metrics)

    # Write a valid first snapshot
    first = _make_snapshot(phase="idle")
    await store.save(first)
    original_content = path.read_text()

    # Simulate disk-full on second write
    second = _make_snapshot(phase="monitoring_agent", active_card_id="PVT_FULL")
    with patch("coordinare.state_store.os.replace", side_effect=OSError("No space left on device")):
        await store.save(second)  # must not raise

    # Original file survives
    assert path.read_text() == original_content
    # Failure metric recorded
    assert metrics.state_write_failures_total._value.get() == 1.0

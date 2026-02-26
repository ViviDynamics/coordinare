from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from coordinare.metrics import CoordinareMetrics
from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    StateLoadError,
    StateStore,
    WorkflowSnapshot,
)


def _make_snapshot(**overrides) -> WorkflowSnapshot:
    defaults = {
        "snapshot_at": datetime.now(UTC),
        "phase": "idle",
    }
    defaults.update(overrides)
    return WorkflowSnapshot(**defaults)


# --- T010: save/load round-trip tests ---


@pytest.mark.asyncio
async def test_save_load_round_trip_idle(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(phase="idle")

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.phase == "idle"
    assert loaded.active_card_id is None
    assert loaded.schema_version == CURRENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_save_load_round_trip_monitoring_agent(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_1",
        active_card_title="Test Card",
        active_card_column="In Progress",
        agent_session_id="sess_abc",
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.phase == "monitoring_agent"
    assert loaded.active_card_id == "PVT_1"
    assert loaded.active_card_title == "Test Card"
    assert loaded.agent_session_id == "sess_abc"


@pytest.mark.asyncio
async def test_save_load_round_trip_blocked(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="blocked",
        active_card_id="PVT_2",
        active_card_title="Blocked Card",
        active_card_column="Blocked",
        open_questions=["What API?", "Which provider?"],
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.phase == "blocked"
    assert loaded.open_questions == ["What API?", "Which provider?"]


@pytest.mark.asyncio
async def test_save_load_round_trip_monitoring_pr(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_pr",
        active_card_id="PVT_3",
        active_card_title="PR Card",
        active_card_column="In Review",
        pr_url="https://github.com/org/repo/pull/42",
        pr_node_id="PR_NODE_1",
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.phase == "monitoring_pr"
    assert loaded.pr_url == "https://github.com/org/repo/pull/42"
    assert loaded.pr_node_id == "PR_NODE_1"


# --- T011: load returns None when absent; verify_writable passes ---


@pytest.mark.asyncio
async def test_load_returns_none_when_file_absent(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "missing.json", metrics=metrics)

    result = await store.load()

    assert result is None


def test_verify_writable_passes_for_writable_dir(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    store.verify_writable()  # Should not raise


# --- T012: metrics recorded on successful save ---


@pytest.mark.asyncio
async def test_save_records_metrics(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot()

    await store.save(snapshot)

    # Histogram should have one observation
    assert metrics.state_write_duration_seconds._sum.get() > 0
    # Gauge should be set to snapshot timestamp
    assert metrics.state_last_written_timestamp._value.get() > 0


# --- T028: corrupt JSON ---


@pytest.mark.asyncio
async def test_load_corrupt_json_raises_state_load_error(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    path.write_text("not valid json {{{")
    store = StateStore(path=path, metrics=metrics)

    with pytest.raises(StateLoadError) as exc_info:
        await store.load()

    assert exc_info.value.reason == "corrupt"


# --- T029: schema version mismatch ---


@pytest.mark.asyncio
async def test_load_schema_mismatch_raises_state_load_error(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot()
    await store.save(snapshot)

    # Manually tamper with schema_version
    import json

    data = json.loads(path.read_text())
    data["schema_version"] = 999
    path.write_text(json.dumps(data))

    with pytest.raises(StateLoadError) as exc_info:
        await store.load()

    assert exc_info.value.reason == "schema_mismatch"


# --- T030: invalid phase value ---


@pytest.mark.asyncio
async def test_load_invalid_phase_raises_state_load_error(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot()
    await store.save(snapshot)

    import json

    data = json.loads(path.read_text())
    data["phase"] = "totally_invalid_phase"
    path.write_text(json.dumps(data))

    with pytest.raises(StateLoadError) as exc_info:
        await store.load()

    assert exc_info.value.reason == "corrupt"


# --- T031: disk full simulation (save catches OSError) ---


@pytest.mark.asyncio
async def test_save_catches_os_error_on_disk_full(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot()

    with patch("coordinare.state_store.os.replace", side_effect=OSError("disk full")):
        await store.save(snapshot)  # Should not raise

    assert metrics.state_write_failures_total._value.get() == 1.0


# --- T032: torn write protection (original file survives) ---


@pytest.mark.asyncio
async def test_torn_write_preserves_original_file(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)

    # Write a valid first snapshot
    first = _make_snapshot(phase="idle")
    await store.save(first)
    original_content = path.read_text()

    # Attempt a second write that fails at os.replace
    second = _make_snapshot(phase="monitoring_agent", active_card_id="PVT_X")
    with patch("coordinare.state_store.os.replace", side_effect=OSError("disk full")):
        await store.save(second)

    # Original file should still be intact
    assert path.read_text() == original_content


# --- T033: verify_writable raises on read-only dir ---


def test_verify_writable_raises_on_read_only_dir(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    read_only = tmp_path / "readonly"
    read_only.mkdir()
    os.chmod(read_only, 0o444)

    store = StateStore(path=read_only / "state.json", metrics=metrics)

    try:
        with pytest.raises(OSError):
            store.verify_writable()
    finally:
        os.chmod(read_only, 0o755)


# --- T039: performance benchmark ---


@pytest.mark.asyncio
async def test_save_completes_within_budget(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_PERF",
        active_card_title="Perf Test Card",
        active_card_column="In Progress",
        agent_session_id="sess_perf",
        open_questions=["q1", "q2", "q3"],
    )

    start = time.monotonic()
    await store.save(snapshot)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"State write took {elapsed:.3f}s, exceeding 1.0s budget (SC-002)"

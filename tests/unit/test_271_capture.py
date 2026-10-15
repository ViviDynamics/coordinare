from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import structlog

from coordinare import configure_logging
from coordinare.__main__ import _run
from coordinare.config import EventStoreConfig
from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from coordinare.dashboard.store import DashboardStore, _Running
from coordinare.graph.nodes.monitor.activity import _record_activity_batch
from coordinare.services.activity_log import ActivityLog
from coordinare.services.event_capture import (
    event_store_processor,
    install_event_store,
    symphony_for_card,
)
from coordinare.services.event_store import EventStore
from tests.unit.dashboard_js_slices import shipped_function


@pytest.fixture
def capture_store(tmp_path: Path) -> Any:
    install_event_store(None)
    store = EventStore(tmp_path / "events.db")
    install_event_store(store, lambda _card: "garden")
    yield store
    install_event_store(None)


@pytest.mark.asyncio
async def test_activity_operator_and_log_funnels(capture_store: EventStore) -> None:
    await capture_store.start()
    log = ActivityLog()
    log.record(activity_type="progress", card_id="card", text="working")
    log.record_many([{"activity_type": "operator_action", "card_id": "card", "text": "cancel"}])
    configure_logging()
    logger = structlog.get_logger("test")
    logger.info("bootstrap.ready", symphony="garden", detail="verified")
    assert event_store_processor(None, "debug", {"event": "invisible"}) == {"event": "invisible"}
    event_store_processor(None, "info", {"event": "event_store.failure"})
    await capture_store.close()
    rows = (await capture_store.query_events())["events"]
    assert {row["kind"] for row in rows} == {"progress", "operator_action", "bootstrap.ready"}
    assert {row["source"] for row in rows} == {"daemon", "operator", "bootstrap"}
    assert all(row["symphony"] == "garden" for row in rows)


@pytest.mark.asyncio
async def test_performer_intake_without_feed_keeps_full_payload(capture_store: EventStore) -> None:
    await capture_store.start()
    state: Any = {
        "event_store": capture_store,
        "current_symphony": "garden",
        "agent_dispatch": {"session_id": "run"},
    }
    events = [
        {"type": kind, "text": kind, "arg": "argument " * 100, "transcript_seq": index}
        for index, kind in enumerate(["tool_use", "progress", "error", "cost"], start=1)
    ]
    _record_activity_batch(state, events, card_id="card", stage="implementer")
    _record_activity_batch(state, events, card_id="card", stage="implementer")
    await capture_store.close()
    rows = (await capture_store.query_events())["events"]
    assert len(rows) == 4
    assert all(row["source"] == "performer" and row["run_id"] == "run" for row in rows)
    assert all(row["data"]["run_id"] == "run" and row["data"]["transcript_seq"] for row in rows)
    assert all(len(row["data"]["arg"]) > 200 for row in rows)


@pytest.mark.asyncio
async def test_failed_store_does_not_recurse(capture_store: EventStore) -> None:
    capture_store.path.mkdir()
    await capture_store.start()
    structlog.get_logger("test").error("daemon.example")
    await capture_store.close()
    assert capture_store.health()["dropped"] == 1


@pytest.mark.asyncio
async def test_capture_redacts_tokens_from_all_funnels(capture_store: EventStore) -> None:
    token = "ghp_" + "a" * 36
    await capture_store.start()
    ActivityLog().record(activity_type="operator_action", text=token)
    event_store_processor(
        None, "error", {"event": "daemon.error", "message": token, "nested": {"password": "plain"}},
    )
    _record_activity_batch(
        {"event_store": capture_store},
        [{"type": "tool_use", "text": token, "args": {"secret": "plain"}}],
        card_id="card",
        stage="implementer",
    )
    await capture_store.close()
    rows = (await capture_store.query_events())["events"]
    assert len(rows) == 3
    assert token not in str(rows)
    assert "plain" not in str(rows)


@pytest.mark.asyncio
async def test_store_failure_cannot_break_logging_or_live_feed(capture_store: EventStore) -> None:
    with patch.object(capture_store, "emit", side_effect=RuntimeError("broken")):
        original = {"event": "daemon.example"}
        assert event_store_processor(None, "warning", original) is original
        assert ActivityLog().record(activity_type="progress", text="visible") is not None
    await capture_store.close()


@pytest.mark.asyncio
async def test_daemon_wrapper_always_flushes_on_startup_failure(tmp_path: Path) -> None:
    install_event_store(None)
    path = tmp_path / "state.json"
    config = SimpleNamespace(state_file_path=path, event_store=EventStoreConfig())

    async def fail(*args: Any) -> None:
        assert args[-1].path == tmp_path / "events.db"
        structlog.get_logger().error("daemon.start_failed")
        raise RuntimeError("start failed")

    with (
        patch("coordinare.__main__._run_with_events", side_effect=fail),
        patch("coordinare.__main__.validate_start_time"),
        pytest.raises(RuntimeError, match="start failed"),
    ):
        await _run(config)
    with sqlite3.connect(tmp_path / "events.db") as db:
        assert db.execute("SELECT kind FROM events").fetchone()[0] == "daemon.start_failed"
    # The processor has been detached, so later log records cannot reach a closed store.
    event_store_processor(None, "info", {"event": "after.close"})


@pytest.mark.asyncio
async def test_health_snapshot_exposes_cached_store_statistics(capture_store: EventStore) -> None:
    await capture_store.start()
    capture_store.emit({"message": "healthy"})
    await capture_store.flush()
    daemon = SimpleNamespace(state={"event_store": capture_store})
    snapshot = DashboardStore._system_health(daemon, [], _Running(0, None, None, 1))
    assert snapshot["event_store"] == capture_store.health()
    assert set(snapshot["event_store"]) == {"size_bytes", "rows", "oldest_ts", "dropped", "pruned", "writer_state", "last_error"}
    await capture_store.close()


def test_health_line_uses_shipped_javascript_and_escapes_values() -> None:
    source = _DASHBOARD_JS_SOURCES["systemhealth"]
    script = "\n".join(
        [
            shipped_function("helpers", "esc"),
            source,
            "const stats = {size_bytes: 10, rows: 2, oldest_ts: '<secret>', dropped: 3, pruned: 4, writer_state: 'failed', last_error: '<error>'};",
            "const html = systemHealthHtml({overall: 'healthy', rows: [], event_store: stats});",
            "if (!html.includes('10 bytes, 2 rows') || !html.includes('3 dropped, 4 pruned') || html.includes('<secret>') || !html.includes('writer failed: &lt;error&gt;')) throw Error(html);",
        ],
    )
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_symphony_resolution_uses_the_cards_owner() -> None:
    state = {
        "current_symphony": "different",
        "active_sessions": {"card": {"current_symphony": "garden"}},
    }
    assert symphony_for_card(state, "card") == "garden"


@pytest.mark.asyncio
async def test_startup_validation_runs_before_event_store_creation() -> None:
    with (
        patch("coordinare.__main__.validate_start_time", side_effect=ValueError("invalid config")),
        patch("coordinare.__main__.EventStore", side_effect=AssertionError("store created too early")),
        pytest.raises(ValueError, match="invalid config"),
    ):
        await _run(SimpleNamespace())

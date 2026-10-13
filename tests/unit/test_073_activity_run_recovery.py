"""Replayed activity must be deduplicated within its actual producer run."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coordinare.dashboard import (
    _ACTIVITY_STREAM_JS,
    _DASHBOARD_HTML,
    _DASHBOARD_JS_SOURCES,
    DashboardStore,
)


def _snapshot(store: DashboardStore) -> dict:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    health = MagicMock()
    health.snapshot.return_value.probes = []
    return store.build_snapshot(daemon, None, health)


def test_activity_run_identity_survives_reconnect_without_metrics() -> None:
    first, second = DashboardStore(), DashboardStore()
    original = _snapshot(first)
    identity = original.get("activity_run_id")
    assert isinstance(identity, str) and identity, "Snapshot must identify the actual activity producer run"
    assert _snapshot(first)["activity_run_id"] == identity
    assert _snapshot(second)["activity_run_id"] != identity
    # 119: the start time no longer depends on a metrics registry being passed.
    assert original["daemon_start_time"]


@pytest.mark.parametrize("case", ["restart", "same", "filter", "quiet", "legacy"])
def test_native_activity_run_browser_recovery(tmp_path: Path, case: str) -> None:
    stores = [DashboardStore(), DashboardStore()]
    wire = []
    for index, store in enumerate(stores):
        store.activity_log.record(activity_type="tool_use", card_id="card", text=f"run-{index}")
        wire.append({"state": _snapshot(store), "entries": store.activity_log.snapshot()})
    assert wire[0]["entries"][0]["seq"] == wire[1]["entries"][0]["seq"] == 0
    wire_path = tmp_path / "wire.json"
    wire_path.write_text(json.dumps(wire))
    start = "// 138: activity feed"
    end = "document.addEventListener('DOMContentLoaded', function() {"
    assert _DASHBOARD_HTML.count(start) == _DASHBOARD_HTML.count(end) == 1
    script = tmp_path / "client.js"
    script.write_text(_DASHBOARD_JS_SOURCES["helpers"] + "\n" + _ACTIVITY_STREAM_JS + "\n" + start + _DASHBOARD_HTML.split(start)[1].split(end)[0])
    node = shutil.which("node")
    assert node is not None, "Node is required for dashboard checks"
    checks = Path(__file__).resolve().parents[1] / "js" / "activity_run_checks.js"
    result = subprocess.run([node, str(checks), str(script), str(wire_path), case], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr

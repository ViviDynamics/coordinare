"""Exercise the shipped stream bootstrap with deterministic browser primitives."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import app_js


@pytest.mark.parametrize("case", ["native", "closed", "backoff", "handlers", "stale"])
def test_dashboard_stream_recovery(tmp_path: Path, case: str) -> None:
    start = "var banner = document.getElementById('disconnected-banner');"
    end = "document.addEventListener('DOMContentLoaded', function() {"
    assert app_js().count(start) == app_js().count(end) == 1
    bootstrap = tmp_path / "bootstrap.js"
    status = re.search(r'function updateConnectionStatus\(label\) \{.*?\n\}', app_js(), re.DOTALL)
    assert status is not None
    # 139: the state listener calls the symphony switcher, which lives in symphonies.js.
    bootstrap.write_text(
        'var _afHasMessage = false, _afLastMsgAt = 0;\n' + status.group(0) + '\n' +
        _DASHBOARD_JS_SOURCES["symphonies"] + '\n' +
        start + app_js().split(start, 1)[1].split(end, 1)[0],
    )
    node = shutil.which("node")
    assert node is not None, "Node is required for the dashboard browser checks"
    checks = Path(__file__).resolve().parents[1] / "js" / "dashboard_reconnect_checks.js"
    result = subprocess.run(
        [node, str(checks), str(bootstrap), case],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {case}" in result.stdout

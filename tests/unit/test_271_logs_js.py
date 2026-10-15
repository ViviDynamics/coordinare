"""Execute the shipped Logs script under node."""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinare.dashboard.assets import _DASHBOARD_JS_ORDER

TIMEOUT_SECONDS = 60
ROOT = Path(__file__).resolve().parents[2]


def test_logs_node_checks() -> None:
    result = subprocess.run(["node", str(ROOT / "tests/js/logs_checks.js")],
                            capture_output=True, text=True, check=False, timeout=TIMEOUT_SECONDS)
    assert result.returncode == 0, result.stdout + result.stderr


def test_logs_asset_is_registered_before_app() -> None:
    assert _DASHBOARD_JS_ORDER.index("logs") < _DASHBOARD_JS_ORDER.index("app")

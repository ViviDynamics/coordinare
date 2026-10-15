"""Execute the shipped Logs script under node."""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinare.dashboard.assets import _DASHBOARD_JS_ORDER, _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import shipped_function, time_helpers_js

TIMEOUT_SECONDS = 60
ROOT = Path(__file__).resolve().parents[2]


def test_logs_node_checks(tmp_path: Path) -> None:
    script = tmp_path / "logs.js"
    script.write_text("\n".join([
        shipped_function("helpers", "esc"), time_helpers_js(),
        shipped_function("runs", "runPathFor"), shipped_function("runs", "runEventHash"),
        shipped_function("cardpage", "cardPathFor"), _DASHBOARD_JS_SOURCES["logs"],
    ]))
    result = subprocess.run(["node", str(ROOT / "tests/js/logs_checks.js"), str(script)],
                            capture_output=True, text=True, check=False, timeout=TIMEOUT_SECONDS)
    assert result.returncode == 0, result.stdout + result.stderr


def test_logs_asset_is_registered_before_app() -> None:
    assert _DASHBOARD_JS_ORDER.index("logs") < _DASHBOARD_JS_ORDER.index("app")

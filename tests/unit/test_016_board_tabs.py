from __future__ import annotations

import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_JS_SOURCES


def test_shipped_board_renderer_isolates_empty_tabs_and_retains_legacy_fallback(tmp_path: Path):
    helpers = tmp_path / "helpers.js"
    performers = tmp_path / "performers.js"
    helpers.write_text(_DASHBOARD_JS_SOURCES["helpers"], encoding="utf-8")
    performers.write_text(_DASHBOARD_JS_SOURCES["performers"], encoding="utf-8")
    checks = Path(__file__).resolve().parents[1] / "js" / "board_tab_checks.js"
    result = subprocess.run(
        ["node", str(checks), str(helpers), str(performers)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUMMARY 7 0" in result.stdout

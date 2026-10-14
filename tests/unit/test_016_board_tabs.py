"""#16 (rewritten for #139): the Cards panel isolates each symphony's board.

The board tabs were the first way to pick one symphony. The header switcher
(#139) replaced them, so the isolation they gave is now the scope applied
before the panel renders. This file keeps the same properties under the new
mechanism.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_JS_SOURCES


def test_the_cards_panel_isolates_each_symphony_under_the_header_choice(tmp_path: Path) -> None:
    helpers = tmp_path / "helpers.js"
    performers = tmp_path / "performers.js"
    symphonies = tmp_path / "symphonies.js"
    helpers.write_text(_DASHBOARD_JS_SOURCES["helpers"], encoding="utf-8")
    # 149: the renderer calls waiting.js, which the page loads right after performers.js.
    performers.write_text(_DASHBOARD_JS_SOURCES["performers"] + "\n" + _DASHBOARD_JS_SOURCES["waiting"], encoding="utf-8")
    symphonies.write_text(_DASHBOARD_JS_SOURCES["symphonies"], encoding="utf-8")
    checks = Path(__file__).resolve().parents[1] / "js" / "board_scope_checks.js"
    result = subprocess.run(
        ["node", str(checks), str(helpers), str(performers), str(symphonies)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUMMARY 9 0" in result.stdout

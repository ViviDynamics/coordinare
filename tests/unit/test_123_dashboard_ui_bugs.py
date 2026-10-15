"""#123, #124, #125, #127, #128: dashboard UI bugs from the 2026-10-13 UX review.

- #123: a role at capacity turned its utilization bar red.
- #124: the performer tile showed a raw ProjectV2 item id.
- #125: two elapsed times for one run disagreed.
- #127: the page scrolled sideways on a phone (covered in the browser suite).
- #128: "Recent cycles" grew into a wall of identical rows.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import label_helpers_js, page_source, time_helpers_js

_CHECKS = Path(__file__).resolve().parents[1] / "js" / "dashboard_ui_bug_checks.js"
_PERFORMERS = _DASHBOARD_JS_SOURCES["performers"]
_HELPERS = _DASHBOARD_JS_SOURCES["helpers"]


def _fn(source: str, name: str) -> str:
    match = re.search(rf"function {name}\(.*?\n\}}", source, re.DOTALL)
    assert match is not None, f"{name} is no longer in the shipped JS"
    return match.group(0)


def _sliced_js() -> str:
    return "\n\n".join(
        [_fn(_HELPERS, "esc"), time_helpers_js(), _fn(_HELPERS, "fmtAge"), label_helpers_js()]
        + [_fn(_PERFORMERS, name) for name in (
            "utilizationBarColor", "utilizationBarHtml", "cardRefHtml",
            "elapsedSinceHtml", "refreshElapsedReadouts", "collapseCycleRows",
        )],
    )


def test_dashboard_ui_bug_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "ui.js"
    js_path.write_text(_sliced_js(), encoding="utf-8")
    result = subprocess.run([node, str(_CHECKS), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 30, f"only {summary.group(1)} checks ran:\n{report}"


def test_utilization_table_uses_the_shared_bar_renderer() -> None:
    """#123, moved by #154: the Performers page table draws the bar, and the template never picks a colour from the percentage."""
    assert "utilizationBarHtml(r) + ' ' + esc(busyText(activeVal, maxVal, 0))" in _PERFORMERS
    assert "role-utilization" not in page_source()
    assert "pct >= 100" not in page_source()


def test_performer_tile_no_longer_prints_the_card_node_id() -> None:
    """#124"""
    assert "Card: <strong>" not in _PERFORMERS
    assert "cardRefHtml(sess)" in _PERFORMERS


def test_elapsed_readouts_share_one_start_point_and_one_timer() -> None:
    """#125"""
    # 154: the Conductor card's "Stage running for" line is gone; the tile's elapsed readout is the one.
    assert "Agent running for" not in page_source()
    assert "Stage running for" not in page_source()
    assert page_source().count("refreshElapsedReadouts(document)") == 1
    assert "elapsedSinceHtml(dispatchAt)" in _DASHBOARD_JS_SOURCES["layout"]


def test_recent_checks_are_collapsed_and_capped_on_the_history_page() -> None:
    """#128 kept the list collapsed; #150 put runs on History, and #154 moved this cycle log there too."""
    assert "renderCycleHistoryInto(document.getElementById('history-cycles'), s.cycle_history, { collapse: true });" in page_source()
    assert "document.getElementById('history-page-section'), s.cycle_history);" not in page_source()
    assert ".cycle-history-compact { max-height: 240px; overflow-y: auto; }" in page_source()
    assert 'id="history-cycles-panel"' in page_source()


def test_the_configuration_tabs_scroll_inside_their_own_container() -> None:
    """#127, kept for the Configuration page's tabs after the board tabs went (#139)."""
    tabs = re.search(r"\.cfg-tabs \{[^}]*\}", page_source())
    assert tabs is not None
    assert "overflow-x: auto" in tabs.group(0) and "flex-wrap: nowrap" in tabs.group(0)
    # The Cards panel has no tab strip any more, so nothing in the page is left to scroll for it.
    assert 'id="swimlane-tabs"' not in page_source()
    assert "swimlane-tab" not in page_source()

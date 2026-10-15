"""#154: the Dashboard layout's pure render functions, run under node.

The status strip's composition and the active-run tile's composition are checked
against the shipped scripts, not copies of them. The browser behaviour is in
tests/e2e/test_154_layout.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import label_helpers_js, time_helpers_js

_CHECKS = Path(__file__).resolve().parents[1] / "js" / "layout_154_checks.js"
# The scripts the tile and the strip call, in the order the page loads them.
_LAYOUT_SCRIPTS = ("helpers", "usage", "waiting", "attention", "systemhealth", "symphonies", "actions", "timeline", "performers", "layout")
_MIN_CHECKS = 28


def _concatenated_js() -> str:
    return "\n\n".join([_DASHBOARD_JS_SOURCES["vocabulary"], label_helpers_js(), time_helpers_js()]
                       + [_DASHBOARD_JS_SOURCES[name] for name in _LAYOUT_SCRIPTS])


def test_the_dashboard_has_no_utilization_table_and_no_recent_checks() -> None:
    """The utilization table and the Recent checks panel moved off the Dashboard (Performers and History)."""
    assert 'id="role-utilization-card"' not in _DASHBOARD_HTML
    assert 'id="recent-cycles-card"' not in _DASHBOARD_HTML
    assert 'id="history-section"' not in _DASHBOARD_HTML
    assert 'id="idle-panel"' not in _DASHBOARD_HTML


def test_the_status_strip_is_above_the_active_runs_and_holds_the_conductor_controls() -> None:
    strip = re.search(r'<section id="status-strip".*?</section>', _DASHBOARD_HTML, re.DOTALL)
    assert strip is not None
    for needle in ('id="phase"', 'id="status-counts"', 'id="spend-strip"', 'id="connection-status"',
                   'id="health-dot"', 'id="force-poll-btn"'):
        assert needle in strip.group(0), needle
    assert _DASHBOARD_HTML.index('id="status-strip"') < _DASHBOARD_HTML.index('id="active-performers"')


def test_the_lower_panels_are_collapsed_details_or_one_line() -> None:
    assert re.search(r'<details class="card" id="metrics-card">', _DASHBOARD_HTML)
    assert re.search(r'<details><summary>Workflow</summary>', _DASHBOARD_HTML)
    assert re.search(r'<details class="card" id="history-cycles-panel">', _DASHBOARD_HTML)


def test_layout_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "layout.js"
    js_path.write_text(_concatenated_js(), encoding="utf-8")
    result = subprocess.run([node, str(_CHECKS), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= _MIN_CHECKS, f"only {summary.group(1)} checks ran:\n{report}"

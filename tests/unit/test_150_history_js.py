"""#150: the History page's client helpers under node, plus the static wiring.

The browser behaviour (the table, the filters, the pager, the link and the scope) is in
tests/e2e/test_150_history.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import _DASHBOARD_JS_ORDER, DASHBOARD_HTML_BUDGET_BYTES
from tests.unit.dashboard_js_slices import shipped_function, shipped_var, time_helpers_js

_REPO = Path(__file__).resolve().parents[2]


def _history_slice() -> str:
    """The helpers history.js needs at load and at call time, verbatim, in front of it."""
    return "\n\n".join([
        shipped_function("helpers", "esc"),
        shipped_function("helpers", "fmtDuration"),
        time_helpers_js(),
        shipped_function("helpers", "fmtCost"),
        _DASHBOARD_JS_SOURCES["usage"],
        shipped_function("runs", "runPathFor"),
        # 233: the card cell's link comes from the card page's address helpers.
        shipped_var("runs", "RUN_NAME_PATTERN"),
        _DASHBOARD_JS_SOURCES["cardpage"],
        _DASHBOARD_JS_SOURCES["symphonies"],
        _DASHBOARD_JS_SOURCES["history"],
    ])


def test_history_checks_under_node(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "history.js"
    js_path.write_text(_history_slice(), encoding="utf-8")
    checks = _REPO / "tests" / "js" / "history_checks.js"
    result = subprocess.run(
        [node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 60, f"only {summary.group(1)} checks ran:\n{report}"


def test_history_js_loads_before_the_application_script() -> None:
    order = list(_DASHBOARD_JS_ORDER)
    assert order.index("history") < order.index("app")
    assert order.index("runs") < order.index("history")


def test_the_history_page_is_fed_by_the_listing_not_the_snapshot() -> None:
    script = _DASHBOARD_JS_SOURCES["history"]
    assert "/api/history/runs" in script
    assert "cycle_history" not in script
    app = _DASHBOARD_JS_SOURCES["app"]
    assert "function renderHistoryPage" not in app


def test_the_dashboard_panel_still_reads_the_snapshot() -> None:
    # 154: the cycle log is the History page's Recent checks panel now, not the Dashboard's.
    assert "renderCycleHistoryInto(document.getElementById('history-cycles')" in _DASHBOARD_JS_SOURCES["app"]


def test_the_page_stays_under_its_size_budget() -> None:
    assert len(_DASHBOARD_HTML.encode("utf-8")) <= DASHBOARD_HTML_BUDGET_BYTES

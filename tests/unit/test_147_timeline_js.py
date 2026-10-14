"""#147: the card pipeline timeline's render functions under node, and the static wiring.

The browser behaviour (the tracker, a stage's runs, the lifecycle list, the board dots and a card
of a symphony the viewer cannot see) is in tests/e2e/test_147_card_timeline.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import _DASHBOARD_JS_ORDER, DASHBOARD_HTML_BUDGET_BYTES
from tests.unit.dashboard_js_slices import (
    label_helpers_js,
    run_id_js,
    shipped_function,
    usage_helpers_js,
)

_REPO = Path(__file__).resolve().parents[2]
_CHECKS = _REPO / "tests" / "js" / "timeline_checks.js"


def _sliced(tmp_path: Path) -> Path:
    """The helpers the timeline calls, verbatim, then the timeline script itself."""
    js_path = tmp_path / "timeline.js"
    js_path.write_text(
        "\n\n".join([
            shipped_function("helpers", "esc"),
            shipped_function("helpers", "fmtTime"),
            shipped_function("helpers", "fmtCost"),
            label_helpers_js(),
            run_id_js(),
            usage_helpers_js(),
            _DASHBOARD_JS_SOURCES["symphonies"],
            _DASHBOARD_JS_SOURCES["timeline"],
        ]),
        encoding="utf-8",
    )
    return js_path


def test_timeline_checks_under_node(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    result = subprocess.run(
        [node, str(_CHECKS), str(_sliced(tmp_path))], capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 50, f"only {summary.group(1)} checks ran:\n{report}"


def test_timeline_js_loads_after_its_helpers_and_before_the_application_script() -> None:
    order = list(_DASHBOARD_JS_ORDER)
    assert order.index("timeline") > order.index("runs")
    assert order.index("timeline") < order.index("symphonies") < order.index("app")


def test_the_page_stays_under_its_size_budget() -> None:
    assert len(_DASHBOARD_HTML.encode("utf-8")) <= DASHBOARD_HTML_BUDGET_BYTES


def test_the_timeline_script_is_es5_with_no_arrow_functions_or_let() -> None:
    source = _DASHBOARD_JS_SOURCES["timeline"]
    assert "=>" not in source
    assert re.search(r"\blet\s", source) is None
    assert re.search(r"\bconst\s", source) is None


def test_the_timeline_read_is_the_scoped_view_and_the_strips_have_a_scope_row() -> None:
    scope = _DASHBOARD_JS_SOURCES["symphonies"]
    assert "card_pipeline: 'scoped'" in scope
    assert "scoped.card_pipeline" in scope

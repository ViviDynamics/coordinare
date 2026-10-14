"""#134: the dashboard's token and cost display, checked under node.

``usage.js`` is loaded verbatim with the two helpers it relies on (``esc`` and
``fmtCost``, sliced from the shipped ``helpers.js``). The node script asserts the
headline text, the tooltip breakdown and the escaping.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import shipped_function

_CHECKS = Path(__file__).resolve().parents[1] / "js" / "usage_checks.js"


def _sliced_js() -> str:
    return "\n\n".join([
        shipped_function("helpers", "esc"),
        shipped_function("helpers", "fmtCost"),
        _DASHBOARD_JS_SOURCES["usage"],
    ])


def test_usage_client_js(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "usage_sliced.js"
    js_path.write_text(_sliced_js(), encoding="utf-8")
    result = subprocess.run([node, str(_CHECKS), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 25, f"only {summary.group(1)} checks ran:\n{report}"


def test_usage_js_is_loaded_before_the_scripts_that_call_it() -> None:
    order = list(_DASHBOARD_JS_SOURCES)
    assert order.index("usage") < order.index("performers") < order.index("app")


def test_the_status_strip_carries_the_spend_and_metrics_no_longer_repeat_it() -> None:
    """#154: the spend strip moved from the header into the status strip; Metrics keeps no spend figures."""
    strip = re.search(r'<section id="status-strip".*?</section>', _DASHBOARD_HTML, re.DOTALL)
    assert strip is not None and 'id="spend-strip"' in strip.group(0)
    assert 'id="spend-today"' not in _DASHBOARD_HTML
    assert 'id="spend-week"' not in _DASHBOARD_HTML


def test_dashboard_html_stays_under_its_budget() -> None:
    from coordinare.dashboard import DASHBOARD_HTML_BUDGET_BYTES

    assert len(_DASHBOARD_HTML.encode()) < DASHBOARD_HTML_BUDGET_BYTES

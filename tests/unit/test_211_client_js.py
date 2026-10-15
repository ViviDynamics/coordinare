"""#211: the activity link's target and the run header's ledger figures, under node.

The browser behaviour (a row opens the exact event, the fallback, the header in a real page) is
in tests/e2e/test_211_exact_links.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import run_id_js, shipped_function, time_helpers_js

_REPO = Path(__file__).resolve().parents[2]
_CHECKS = _REPO / "tests" / "js" / "transcript_link_checks.js"


def _sliced_js() -> str:
    # esc, fmtCost and the run id helpers (141) are the only helpers.js functions the link and the
    # header call. The rest of helpers.js reads the document when it loads, so it is not evaluated here.
    return "\n\n".join([
        shipped_function("helpers", "esc"),
        time_helpers_js(),
        shipped_function("helpers", "fmtCost"),
        run_id_js(),
        _DASHBOARD_JS_SOURCES["usage"],
        _DASHBOARD_JS_SOURCES["runs"],
    ])


def test_link_and_header_checks_under_node(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "transcript_link.js"
    js_path.write_text(_sliced_js(), encoding="utf-8")
    result = subprocess.run(
        [node, str(_CHECKS), str(js_path)], capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 30, f"only {summary.group(1)} checks ran:\n{report}"


def test_the_runs_script_no_longer_reads_the_removed_summary_keys() -> None:
    runs_js = _DASHBOARD_JS_SOURCES["runs"]
    assert "sum.tokens" not in runs_js
    assert "sum.cost" not in runs_js
    assert "sum.usage" in runs_js

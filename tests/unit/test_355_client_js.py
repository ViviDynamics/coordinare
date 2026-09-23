"""355: gate for the dashboard's inlined Open Questions / Clarification History JS.

Slices the two panel blocks verbatim out of ``_DASHBOARD_HTML`` (plus the
``esc`` and ``fmtAge`` helpers they call) and executes them under node against
a DOM stub (``tests/js/question_panels_checks.js``) — the same pattern as the
spec-138 activity-feed gate: no browser, no npm install, no timers.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinare.dashboard import _DASHBOARD_HTML

_CHECKS_JS = Path(__file__).resolve().parents[1] / "js" / "question_panels_checks.js"

# Slice boundaries. Each must match exactly once — a marker that drifts would
# silently shrink the slice and leave the checks running against nothing, so
# `_extract_panels_js` raises rather than returning a partial block.
_START_Q = "// Open questions (355"
_START_CL = "// Clarification history (355"
_END = "document.getElementById('cycles-completed')"


def _extract_panels_js() -> str:
    """Return the shipped panel JS plus the shared helpers it calls."""
    for marker in (_START_Q, _START_CL, _END):
        if _DASHBOARD_HTML.count(marker) != 1:
            raise AssertionError(f"marker {marker!r} is not unique in _DASHBOARD_HTML")

    start_q = _DASHBOARD_HTML.index(_START_Q)
    start_cl = _DASHBOARD_HTML.index(_START_CL)
    end = _DASHBOARD_HTML.index(_END)
    panels_js = _DASHBOARD_HTML[start_q:start_cl] + _DASHBOARD_HTML[start_cl:end]

    esc = re.search(r"function esc\(s\) \{.*?\n\}", _DASHBOARD_HTML, re.DOTALL)
    if esc is None:
        raise AssertionError("the shared esc() helper is no longer in _DASHBOARD_HTML")
    fmt_age = re.search(r"function fmtAge\(iso\) \{.*?\n\}", _DASHBOARD_HTML, re.DOTALL)
    if fmt_age is None:
        raise AssertionError("the shared fmtAge() helper is no longer in _DASHBOARD_HTML")
    return (
        esc.group(0) + "\n\n" + fmt_age.group(0) + "\n\n"
        + "function renderQuestionPanels(s) {\n" + panels_js + "\n}\n"
    )


def test_panels_js_slice_is_complete() -> None:
    """A drifted marker must fail loudly, not quietly shrink the slice."""
    panels_js = _extract_panels_js()
    assert "function renderQuestionPanels(s)" in panels_js
    assert "function esc(s)" in panels_js
    assert "function fmtAge(iso)" in panels_js
    assert "cycles-completed" not in panels_js.split("function renderQuestionPanels")[1], (
        "slice overran into the metrics block"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed — the JS gate cannot run")
def test_question_panels_client_js(tmp_path: Path) -> None:
    """Run the shipped panel JS under node. Any failed check fails this test."""
    panels_path = tmp_path / "panels.js"
    panels_path.write_text(_extract_panels_js(), encoding="utf-8")

    result = subprocess.run(
        ["node", str(_CHECKS_JS), str(panels_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"

    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line — the harness did not finish:\n{report}"
    passed, failed = int(summary.group(1)), int(summary.group(2))
    assert failed == 0, report
    # Guards against a harness that loads but silently checks nothing.
    assert passed >= 20, f"only {passed} checks ran — the harness was gutted:\n{report}"

"""354: gate for the dashboard's queued-for-slot board-row presentation.

Slices the pure ``queuedPhaseLabel`` helper from the inlined dashboard JS and
executes it under node (same technique as test_138_client_js.py — no browser,
no npm install). The acceptance rule: a card waiting on a saturated pool
never renders as "Dispatching"; it renders "Queued for <stage>".
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinare.dashboard import _DASHBOARD_HTML

_CHECKS_JS = Path(__file__).resolve().parents[1] / "js" / "queued_board_checks.js"

_QUEUED_FN_START = "// 354: queued-for-slot board-row presentation"
_QUEUED_FN = "function queuedPhaseLabel"


def _extract_queued_js() -> str:
    """Return the pure queued-label helper plus the formatters it calls."""
    if _DASHBOARD_HTML.count(_QUEUED_FN_START) != 1:
        raise AssertionError(f"marker {_QUEUED_FN_START!r} is not unique in _DASHBOARD_HTML")
    if _DASHBOARD_HTML.count(_QUEUED_FN) != 1:
        raise AssertionError(f"{_QUEUED_FN} is not unique in _DASHBOARD_HTML")

    marker = _DASHBOARD_HTML.index(_QUEUED_FN_START)
    start = _DASHBOARD_HTML.index("function queuedPhaseLabel", marker)
    end = _DASHBOARD_HTML.index("}", start) + 1
    queued_js = _DASHBOARD_HTML[start:end]

    esc = re.search(r"function esc\(s\) \{.*?\n\}", _DASHBOARD_HTML, re.DOTALL)
    if esc is None:
        raise AssertionError("the shared esc() helper is no longer in _DASHBOARD_HTML")
    fmt = re.search(r"function humanPhase\(phase\) \{.*?\n\}", _DASHBOARD_HTML, re.DOTALL)
    if fmt is None:
        raise AssertionError("the shared humanPhase() helper is no longer in _DASHBOARD_HTML")
    alias = "var formatPhaseLabel = humanPhase;"
    if alias not in _DASHBOARD_HTML:
        raise AssertionError("the formatPhaseLabel alias is no longer in _DASHBOARD_HTML")
    return esc.group(0) + "\n\n" + fmt.group(0) + "\n\n" + alias + "\n\n" + queued_js


def test_queued_js_slice_is_complete() -> None:
    js = _extract_queued_js()
    assert "function queuedPhaseLabel" in js
    assert "function humanPhase" in js
    assert "var formatPhaseLabel = humanPhase;" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed — the JS gate cannot run")
def test_queued_board_client_js(tmp_path: Path) -> None:
    """Run the shipped queued-label helper under node."""
    js_path = tmp_path / "queued.js"
    js_path.write_text(_extract_queued_js(), encoding="utf-8")

    result = subprocess.run(
        ["node", str(_CHECKS_JS), str(js_path)],
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
    assert passed >= 5, f"only {passed} checks ran — the harness was gutted:\n{report}"

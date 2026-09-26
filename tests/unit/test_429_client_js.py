"""429: gate for the session view's observer-verdicts panel.

Slices ``renderCardDetailContent`` from the shipped performers.js plus the
shared helpers it calls, and executes both under node (same technique as
test_138_client_js.py — no browser, no npm install). The acceptance rule: the
performer/session view renders the recent observer verdicts with their evidence
summaries, escaped, newest first.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinare.dashboard import _DASHBOARD_JS_SOURCES

_CHECKS_JS = Path(__file__).resolve().parents[1] / "js" / "observer_detail_checks.js"

_DETAIL_START = "function renderCardDetailContent"
_NEXT_FN = "function showPerformerDetail"

_HELPERS_JS = _DASHBOARD_JS_SOURCES["helpers"]


def _extract_detail_js() -> str:
    """Return the card-detail renderer plus the shared helpers it calls."""
    performers = _DASHBOARD_JS_SOURCES["performers"]
    if performers.count(_DETAIL_START) != 1:
        raise AssertionError(f"{_DETAIL_START} is not unique in performers.js")
    if performers.count(_NEXT_FN) != 1:
        raise AssertionError(f"{_NEXT_FN} is not unique in performers.js")

    start = performers.index(_DETAIL_START)
    end = performers.index(_NEXT_FN, start)
    detail_js = performers[start:end]
    if "STALE_THRESHOLD_MS" not in detail_js:
        raise AssertionError("the renderer no longer references STALE_THRESHOLD_MS")

    esc = re.search(r"function esc\(s\) \{.*?\n\}", _HELPERS_JS, re.DOTALL)
    if esc is None:
        raise AssertionError("the shared esc() helper is no longer in helpers.js")
    fmt = re.search(r"function humanPhase\(phase\) \{.*?\n\}", _HELPERS_JS, re.DOTALL)
    if fmt is None:
        raise AssertionError("the shared humanPhase() helper is no longer in helpers.js")
    alias = "var formatPhaseLabel = humanPhase;"
    if alias not in _HELPERS_JS:
        raise AssertionError("the formatPhaseLabel alias is no longer in helpers.js")
    fmt_age = re.search(r"function fmtAge\(iso\) \{.*?\n\}", _HELPERS_JS, re.DOTALL)
    if fmt_age is None:
        raise AssertionError("the shared fmtAge() helper is no longer in helpers.js")
    stubs = "var STALE_THRESHOLD_MS = 30 * 60 * 1000;"
    return "\n".join([esc.group(0), fmt.group(0), alias, fmt_age.group(0), stubs, detail_js])


def test_detail_js_slice_is_complete() -> None:
    js = _extract_detail_js()
    assert "function renderCardDetailContent" in js
    assert "observer_verdicts" in js, "the panel must read the session's verdicts"
    assert "function showPerformerDetail" not in js, "slice must stop at the next function"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed — the JS gate cannot run")
def test_observer_detail_client_js(tmp_path: Path) -> None:
    """Run the shipped detail renderer under node."""
    js_path = tmp_path / "detail.js"
    js_path.write_text(_extract_detail_js(), encoding="utf-8")

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
    assert passed >= 8, f"only {passed} checks ran — the harness was gutted:\n{report}"

"""138: gate for the dashboard's inlined activity-feed JavaScript.

Combines the inline feed JS and shipped activity-stream script and executes them under
node against a DOM stub (``tests/js/activity_feed_checks.js``), covering the
quickstart checks C1, C2, C4, C6-C11 and the timing logic behind C3.

Why this exists: the plan's Complexity Tracking recorded the inlined front end
as manually verified, on the grounds that a JS runner would mean a new
dependency plus a timing-sensitive browser suite. This is neither — no browser,
no npm install, no timers, and node is already required by spec 124 (openwiki).
Rendering, screen-reader announcement behaviour and keyboard focus remain out
of scope and stay on the manual checklist (T049a).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinare.dashboard import _ACTIVITY_STREAM_JS, _DASHBOARD_HTML

_CHECKS_JS = Path(__file__).resolve().parents[1] / "js" / "activity_feed_checks.js"

# Slice boundaries. Both must match exactly once — a marker that drifts would
# silently shrink the slice and leave the checks running against nothing, so
# `_extract_feed_js` raises rather than returning a partial block.
_FEED_START = "// 138: activity feed"
_FEED_END = "var banner = document.getElementById('disconnected-banner');"

_EXPECTED_FUNCTIONS = frozenset({
    "esc", "afLabel", "afTime", "afRowHtml", "afMatches", "afAppend", "afRender",
    "afUpdateEmpty", "afUpdateFilterOptions", "onActivityFilterChange",
    "afSetLive", "afNoteMessage", "afQuietTick", "afTick", "afPreview",
})


def _extract_feed_js() -> str:
    """Return the shipped feed JS plus the shared `esc` helper it calls."""
    if _DASHBOARD_HTML.count(_FEED_START) != 1:
        raise AssertionError(f"marker {_FEED_START!r} is not unique in _DASHBOARD_HTML")
    if _DASHBOARD_HTML.count(_FEED_END) != 1:
        raise AssertionError(f"marker {_FEED_END!r} is not unique in _DASHBOARD_HTML")

    marker = _DASHBOARD_HTML.index(_FEED_START)
    start = _DASHBOARD_HTML.rindex("// ---", 0, marker)
    end = _DASHBOARD_HTML.index(_FEED_END)
    feed_js = _DASHBOARD_HTML[start:end]

    esc = re.search(r"function esc\(s\) \{.*?\n\}", _DASHBOARD_HTML, re.DOTALL)
    if esc is None:
        raise AssertionError("the shared esc() helper is no longer in _DASHBOARD_HTML")
    return esc.group(0) + "\n\n" + _ACTIVITY_STREAM_JS + "\n" + feed_js


def test_feed_js_slice_is_complete() -> None:
    """A drifted marker must fail loudly, not quietly shrink the slice."""
    feed_js = _extract_feed_js()
    found = set(re.findall(r"^function (\w+)", feed_js, re.MULTILINE))
    assert found >= _EXPECTED_FUNCTIONS, f"missing from the slice: {sorted(_EXPECTED_FUNCTIONS - found)}"
    assert "var banner" not in feed_js, "slice overran into the EventSource block"


def test_empty_state_names_the_current_daemon_run() -> None:
    """C11 / FR-018: an empty feed after a restart must not read as 'nothing
    is happening'. Asserted here rather than in JS — it is static markup."""
    assert "covers only the current daemon run" in _DASHBOARD_HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed — the JS gate cannot run")
def test_activity_feed_client_js(tmp_path: Path) -> None:
    """Run the shipped feed JS under node. Any failed check fails this test."""
    feed_path = tmp_path / "feed.js"
    feed_path.write_text(_extract_feed_js(), encoding="utf-8")

    result = subprocess.run(
        ["node", str(_CHECKS_JS), str(feed_path)],
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
    assert passed >= 40, f"only {passed} checks ran — the harness was gutted:\n{report}"

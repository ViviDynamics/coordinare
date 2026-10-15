"""237 and 239: the performer detail page's renderers, run under node and pinned against the shared feed.

The renderers are the shipped functions, sliced verbatim: activity_streams.js (the feed's row
helpers, now shared) and performers.js (the detail page). Driven by tests/js/performer_detail_237_checks.js.
The browser behaviour is covered by tests/e2e/test_237_performer_detail.py.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from coordinare.dashboard import _ACTIVITY_STREAM_JS, _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import run_id_js, shipped_function, time_helpers_js

_CHECKS_JS = Path(__file__).resolve().parents[1] / "js" / "performer_detail_237_checks.js"

# The number of checks the harness must run. A harness that loads but checks nothing fails here.
_MIN_CHECKS = 50


def _detail_source() -> str:
    """The detail page's renderer and the 237/239 helpers above it, and nothing else in performers.js."""
    performers = _DASHBOARD_JS_SOURCES["performers"]
    start = performers.index("// 237: the Live Events list.")
    end = performers.index("// 146: the run detail is addressed")
    return shipped_function("performers", "renderPerformersPage") + "\n" + performers[start:end]


def _source() -> str:
    run_pattern = re.search(r"^var RUN_NAME_PATTERN = .*;$", _DASHBOARD_JS_SOURCES["runs"], re.MULTILINE)
    if run_pattern is None:
        raise AssertionError("RUN_NAME_PATTERN is no longer declared in runs.js")
    return "\n\n".join([
        shipped_function("helpers", "esc"),
        shipped_function("helpers", "fmtBytes"),
        time_helpers_js(),
        run_id_js(),
        shipped_function("runs", "runEntryLinkHtml"),
        run_pattern.group(0),
        _ACTIVITY_STREAM_JS,
        _DASHBOARD_JS_SOURCES["performers"],
    ])


def test_the_performer_detail_checks_pass_under_node(tmp_path: Path) -> None:
    source_path = tmp_path / "performer_detail.js"
    source_path.write_text(_source(), encoding="utf-8")

    result = subprocess.run(
        ["node", str(_CHECKS_JS), str(source_path)],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "TZ": "UTC"},
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"performer detail checks failed:\n{report}"

    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line, the harness did not finish:\n{report}"
    passed, failed = int(summary.group(1)), int(summary.group(2))
    assert failed == 0, report
    assert passed >= _MIN_CHECKS, f"only {passed} checks ran:\n{report}"


def test_the_performer_page_renders_its_events_through_the_feed_row() -> None:
    performers = _DASHBOARD_JS_SOURCES["performers"]
    assert "afEventRowHtml(afEntryFromBackend(" in performers
    # The raw wire-type label is gone from the page.
    assert "esc(ev.type" not in performers
    assert "tool_use:" not in performers


def test_the_feed_and_the_performer_page_draw_a_block_through_one_function() -> None:
    assert _ACTIVITY_STREAM_JS.count("function afBlockHtml(") == 1
    assert "blocks.map(afBlockHtml)" in _ACTIVITY_STREAM_JS
    assert "afBlockHtml(afBlockFromEntry(e))" in _ACTIVITY_STREAM_JS


def test_the_feed_labels_live_in_the_shared_renderer_only() -> None:
    assert _ACTIVITY_STREAM_JS.count("var AF_LABELS = {") == 1
    assert "var AF_LABELS" not in _DASHBOARD_JS_SOURCES["app"]
    assert "function afLabel(" not in _DASHBOARD_JS_SOURCES["app"]


def test_the_detail_page_has_no_placeholder_or_code_fallback_strings() -> None:
    performers = _detail_source()
    for text in (
        "No run stats available yet",
        "No live metrics available yet",
        "No live events yet",
        "No stderr logs yet",
        "'performer'",
        "'unknown'",
        "'-'",
        "'event'",
        "PID ' + String(metrics.pid) +",
    ):
        assert text not in performers, f"placeholder still in performers.js: {text}"


def test_the_role_name_is_the_detail_heading_and_the_page_heading_steps_aside() -> None:
    assert _DASHBOARD_HTML.count('id="performers-page-heading"') == 1
    performers = _DASHBOARD_JS_SOURCES["performers"]
    assert "getElementById('performers-page-heading')" in performers
    assert "performer-detail-head" in performers


def test_the_probe_filter_is_narrow_and_explicit() -> None:
    performers = _DASHBOARD_JS_SOURCES["performers"]
    match = re.search(r"var PERFORMER_PROBE_LOG_PATTERNS = \[(.*?)\];", performers)
    assert match is not None
    patterns = match.group(1)
    assert "method=HEAD" in patterns
    assert r"api\/hello" in patterns
    # Model traffic is never a probe.
    assert "v1/messages" not in patterns

"""135: the truncation line's wording and its absence, checked by running the shipped JS under node.

The functions are sliced verbatim from the shipped scripts (activity_streams.js for the
line itself, runs.js for the transcript link, helpers.js for esc, app.js for AF_MAX_ROWS),
so the check runs what the browser runs. Driven by tests/js/truncation_checks.js.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from coordinare.dashboard import _ACTIVITY_STREAM_JS, _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import app_js, shipped_function

_CHECKS_JS = Path(__file__).resolve().parents[1] / "js" / "truncation_checks.js"


def _source() -> str:
    esc = re.search(r"function esc\(s\) \{.*?\n\}", _DASHBOARD_JS_SOURCES["helpers"], re.DOTALL)
    if esc is None:
        raise AssertionError("the shared esc() helper is no longer in helpers.js")
    keep = re.search(r"^var AF_MAX_ROWS = \d+;", app_js(), re.MULTILINE)
    if keep is None:
        raise AssertionError("AF_MAX_ROWS is no longer declared in app.js")
    return "\n\n".join([
        esc.group(0),
        keep.group(0),
        shipped_function("runs", "runEntryLinkHtml"),
        _ACTIVITY_STREAM_JS,
    ])


def test_the_truncation_line_checks_pass_under_node(tmp_path: Path) -> None:
    """Every wording, link and absence check in truncation_checks.js passes."""
    source_path = tmp_path / "truncation.js"
    source_path.write_text(_source(), encoding="utf-8")

    result = subprocess.run(
        ["node", str(_CHECKS_JS), str(source_path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"truncation line checks failed:\n{report}"

    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line, the harness did not finish:\n{report}"
    passed, failed = int(summary.group(1)), int(summary.group(2))
    assert failed == 0, report
    # Guards against a harness that loads but checks nothing.
    assert passed >= 20, f"only {passed} checks ran:\n{report}"


def test_the_raw_output_label_is_replaced_with_plain_words() -> None:
    assert "Raw output" not in _ACTIVITY_STREAM_JS
    assert "may contain other languages" not in _ACTIVITY_STREAM_JS
    assert "Agent output" in _ACTIVITY_STREAM_JS


def test_the_expanded_group_renders_a_notes_slot_that_live_updates_refresh() -> None:
    """The line sits in its own slot, so a streaming group can refresh it without re-rendering the output."""
    assert "af-notes" in _ACTIVITY_STREAM_JS
    assert "notes.innerHTML = afCutHtml(g.entries)" in _ACTIVITY_STREAM_JS

"""148: the activity feed's detail rendering and kind filters, run under node.

The checks execute the shipped ``activity_streams.js`` verbatim, with the shared
``esc()`` helper it depends on, against stand-ins for the page's own helpers.
The end-to-end behaviour (filters persisted across a reload, a hostile command
on a live page) is covered in ``tests/e2e/test_148_activity_feed_detail.py``.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _ACTIVITY_STREAM_JS
from tests.unit.dashboard_js_slices import run_id_js, shipped_function

_CHECKS_JS = Path(__file__).resolve().parents[1] / "js" / "activity_detail_checks.js"


def _sliced_js(tmp_path: Path) -> Path:
    path = tmp_path / "activity_detail.js"
    path.write_text(
        "\n\n".join([
            shipped_function("helpers", "esc"),
            run_id_js(),
            shipped_function("runs", "runEntryLinkHtml"),
            _ACTIVITY_STREAM_JS,
        ]),
        encoding="utf-8",
    )
    return path


def test_the_feed_script_defines_the_148_surface() -> None:
    expected = {
        "afRowKind", "afVisible", "afStreamType", "afLine", "afPlainRowHtml",
        "afLoadChips", "afSaveChips", "onActivityChipToggle", "afApplyChipState",
    }
    found = set(re.findall(r"^function (\w+)", _ACTIVITY_STREAM_JS, re.MULTILINE))
    assert found >= expected, f"missing: {sorted(expected - found)}"


def test_the_chips_in_the_markup_match_the_kinds_the_script_filters_on() -> None:
    markup = (Path(__file__).resolve().parents[2] / "src/coordinare/dashboard/templates/dashboard.html").read_text(
        encoding="utf-8",
    )
    for chip in ("lifecycle", "agent", "tools", "errors"):
        assert f'data-chip="{chip}"' in markup, f"no chip for {chip}"
    assert "onActivityChipToggle(this)" in markup


def test_activity_detail_client_js(tmp_path: Path) -> None:
    """Run the shipped detail rendering under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    result = subprocess.run(
        [node, str(_CHECKS_JS), str(_sliced_js(tmp_path))],
        capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 40, f"only {summary.group(1)} checks ran:\n{report}"

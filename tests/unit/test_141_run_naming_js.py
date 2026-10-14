"""#141: the run name helpers, the attempt wording and the copy action, run under node."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import label_helpers_js, run_id_js, shipped_function

_CHECKS_JS = Path(__file__).resolve().parents[1] / "js" / "run_naming_checks.js"


def _sliced_js(tmp_path: Path) -> Path:
    path = tmp_path / "run_naming.js"
    path.write_text(
        "\n\n".join([
            label_helpers_js(),
            shipped_function("helpers", "esc"),
            run_id_js(),
            shipped_function("helpers", "copyRunId"),
            shipped_function("helpers", "runCopyBySelection"),
            shipped_function("helpers", "runNumberText"),
        ]),
        encoding="utf-8",
    )
    return path


def test_run_naming_checks_under_node(tmp_path: Path) -> None:
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
    assert int(summary.group(1)) >= 20, f"only {summary.group(1)} checks ran:\n{report}"


def test_the_id_helpers_live_in_helpers_js() -> None:
    """helpers.js loads first, so every later script can call them."""
    assert "function runIdHtml(" in _DASHBOARD_JS_SOURCES["helpers"]
    assert "function copyRunId(" in _DASHBOARD_JS_SOURCES["helpers"]
    assert "function runNumberText(" in _DASHBOARD_JS_SOURCES["helpers"]


def test_the_copy_button_calls_the_one_copy_action() -> None:
    """The Copy button calls copyRunId, which holds the clipboard fallback."""
    assert "copyRunId(this)" in _DASHBOARD_JS_SOURCES["helpers"]

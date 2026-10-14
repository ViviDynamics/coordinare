"""#122: the token count read 0 for the whole time a stage was running.

Most backends report usage when the stage ends, so a 0 during the run was
false and made the numbers untrustworthy. A running stage with nothing
reported now shows an explicit pending state; live streaming of usage is #134.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import page_source, usage_helpers_js

_PERFORMERS = _DASHBOARD_JS_SOURCES["performers"]
_HELPERS = _DASHBOARD_JS_SOURCES["helpers"]
_CHECKS = Path(__file__).resolve().parents[1] / "js" / "usage_pending_checks.js"


def _fn(source: str, name: str) -> str:
    match = re.search(rf"function {name}\(.*?\n\}}", source, re.DOTALL)
    assert match is not None, f"{name} is no longer in the shipped JS"
    return match.group(0)


def _sliced_js() -> str:
    note = re.search(r"var USAGE_PENDING_NOTE = .*?;", _PERFORMERS)
    assert note is not None
    return "\n\n".join(
        [_fn(_HELPERS, "esc"), _fn(_HELPERS, "fmtCost"), note.group(0), usage_helpers_js()]
        + [_fn(_PERFORMERS, name) for name in (
            "parseTokenCount", "derivePerformerTokenTotal", "estimateCost",
            "isStageRunning", "tokenReadout", "costHtml",
        )],
    )


def test_usage_pending_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "usage.js"
    js_path.write_text(_sliced_js(), encoding="utf-8")
    result = subprocess.run([node, str(_CHECKS), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 20, f"only {summary.group(1)} checks ran:\n{report}"


def test_metrics_card_uses_the_pending_readout() -> None:
    assert "tokenReadout(cardTokensTotal, !!activeSess)" in page_source()
    assert "cardTokensTotal.toLocaleString();" not in page_source()
    assert "costEl.textContent = tokenView.pending ? '-' : costReadout.text;" in page_source()


def test_performer_detail_uses_the_pending_readout() -> None:
    assert "tokenReadout(tokenTotal, isStageRunning(sel))" in _PERFORMERS

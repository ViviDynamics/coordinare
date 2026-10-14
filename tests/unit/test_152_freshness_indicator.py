"""#152: the header says how fresh the data on screen is.

"Live · updated 3s ago · next check in 18s", and an amber stale state
("no update for 2m: reconnecting…") when the event stream is quiet.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_HTML
from coordinare.dashboard.assets import _DASHBOARD_SHELL_CSS
from tests.unit.dashboard_js_slices import shipped_function


def test_header_has_the_freshness_slot_and_the_page_refreshes_it_every_second() -> None:
    assert '<span id="connection-label">Connecting</span><span id="connection-detail"></span>' in _DASHBOARD_HTML
    assert "refreshElapsedReadouts(document); refreshFreshness(); }, 1000);" in _DASHBOARD_HTML


def test_stale_state_is_styled_amber() -> None:
    rule = re.search(r"#connection-status\.conn-stale[^{]*\{[^}]*\}", _DASHBOARD_SHELL_CSS)
    assert rule is not None and "--color-accent-yellow" in rule.group(0)


def test_freshness_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "freshness.js"
    js_path.write_text(
        "\n\n".join(shipped_function("helpers", name) for name in (
            "nextCheckSeconds", "fmtShortAge", "freshnessText", "refreshFreshness",
        )),
        encoding="utf-8",
    )
    checks = Path(__file__).resolve().parents[1] / "js" / "freshness_checks.js"
    result = subprocess.run([node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 17, f"only {summary.group(1)} checks ran:\n{report}"

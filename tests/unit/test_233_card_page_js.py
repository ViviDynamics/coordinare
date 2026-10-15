"""#233: the card page's pure helpers under node, and the static rules its script follows.

The browser behaviour (the History and Done links, the live card's link, the timeline on the
page, back and forward, the scoped viewer, an unknown and a hostile address) is in
tests/e2e/test_233_card_page.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import shipped_function, shipped_var

_REPO = Path(__file__).resolve().parents[2]
_CHECKS = _REPO / "tests" / "js" / "card_page_233_checks.js"


def _sliced(tmp_path: Path) -> Path:
    """The helpers the card page calls at load and at call time, verbatim, then the card page script."""
    js_path = tmp_path / "cardpage.js"
    js_path.write_text(
        "\n\n".join([
            shipped_function("helpers", "esc"),
            shipped_var("runs", "RUN_NAME_PATTERN"),
            _DASHBOARD_JS_SOURCES["cardpage"],
        ]),
        encoding="utf-8",
    )
    return js_path


def test_card_page_checks_under_node(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    result = subprocess.run(
        [node, str(_CHECKS), str(_sliced(tmp_path))], capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 30, f"only {summary.group(1)} checks ran:\n{report}"


def test_the_card_page_script_is_es5() -> None:
    source = _DASHBOARD_JS_SOURCES["cardpage"]
    assert "=>" not in source
    assert re.search(r"\blet\s", source) is None
    assert re.search(r"\bconst\s", source) is None
    assert "`" not in source


def test_the_card_page_reads_no_snapshot_directly() -> None:
    # Every read of the full snapshot goes through the scoped view (139), so the scope lint stays clean.
    assert "_lastState" not in _DASHBOARD_JS_SOURCES["cardpage"]


def test_the_card_page_script_has_no_dash_punctuation() -> None:
    source = _DASHBOARD_JS_SOURCES["cardpage"]
    assert "\u2014" not in source and "\u2013" not in source


def test_the_card_page_escapes_every_interpolated_value() -> None:
    source = _DASHBOARD_JS_SOURCES["cardpage"]
    for needle in ("addr.card", "addr.symphony", "note"):
        assert f"esc({needle})" in source, needle


def test_the_app_routes_the_card_page_and_keeps_its_own_address() -> None:
    app = _DASHBOARD_JS_SOURCES["app"]
    assert "'card-page'" in app
    assert "isCardPath(path)" in app
    assert "cardPageRoute(path)" in app
    assert "cardPageRefresh()" in app


def test_the_done_lane_and_the_history_row_link_to_the_card_page() -> None:
    assert "cardPathFor(e.symphony, e.iid)" in _DASHBOARD_JS_SOURCES["performers"]
    assert "cardPathFor(row.symphony, row.card)" in _DASHBOARD_JS_SOURCES["history"]


def test_the_live_card_detail_has_a_full_timeline_link() -> None:
    assert "cardFullTimelineHtml(pipelineSymphony" in _DASHBOARD_JS_SOURCES["performers"]

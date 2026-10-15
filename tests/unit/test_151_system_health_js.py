"""#151: the System health card's rendering, checked under node, and its wiring into the page.

``systemhealth.js`` is loaded verbatim with the two helpers it relies on (``esc`` and
``fmtAge``, sliced from the shipped ``helpers.js``). The node script asserts the one-line
summary, the open state, the freshness markup and the escaping.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import _DASHBOARD_JS_DIR
from tests.unit.dashboard_js_slices import app_js, shipped_function, time_helpers_js

_CHECKS = Path(__file__).resolve().parents[1] / "js" / "system_health_checks.js"
_STATIC = _DASHBOARD_JS_DIR


def _sliced_js() -> str:
    return "\n\n".join([
        shipped_function("helpers", "esc"),
        time_helpers_js(),
        shipped_function("helpers", "fmtAge"),
        _DASHBOARD_JS_SOURCES["systemhealth"],
    ])


def test_system_health_card_js(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "system_health_sliced.js"
    js_path.write_text(_sliced_js(), encoding="utf-8")
    result = subprocess.run([node, str(_CHECKS), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 30, f"only {summary.group(1)} checks ran:\n{report}"


def test_system_health_js_loads_after_helpers_and_before_app() -> None:
    order = list(_DASHBOARD_JS_SOURCES)
    assert order.index("helpers") < order.index("systemhealth") < order.index("app")


def test_app_renders_the_card_from_the_snapshot_and_keeps_the_legacy_path() -> None:
    source = app_js()
    assert "if (s.system_health) {" in source
    assert "systemHealthHtml(s.system_health)" in source
    # The subsystem table stays for snapshots that predate the card.
    assert "} else if (!s.subsystems || s.subsystems.length === 0) {" in source


def test_system_health_is_a_shared_scope_row() -> None:
    source = (_STATIC / "symphonies.js").read_text(encoding="utf-8")
    # 266: the row is written per symphony (symphonyHealthScope), so it is a derived row, not a shared one.
    assert re.search(r"^\s*system_health: 'derived',", source, re.MULTILINE)


def test_the_card_styles_ship_with_the_shell_css() -> None:
    css = (_STATIC / "shell.css").read_text(encoding="utf-8")
    for selector in (".sh-row", ".sh-reason", ".sh-healthy", ".sh-down"):
        assert selector in css


def test_dashboard_html_stays_under_its_budget() -> None:
    from coordinare.dashboard import DASHBOARD_HTML_BUDGET_BYTES

    assert len(_DASHBOARD_HTML.encode()) < DASHBOARD_HTML_BUDGET_BYTES

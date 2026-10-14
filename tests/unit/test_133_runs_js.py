"""#133: the transcript viewer's client helpers under node, plus the static wiring.

The browser behaviour (deep links, expansion, filters, the 2,000-event budget and the
hostile fixture in a real page) is in tests/e2e/test_133_transcript_viewer.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import _DASHBOARD_JS_ORDER, DASHBOARD_HTML_BUDGET_BYTES
from coordinare.services import run_viewer
from tests.unit.dashboard_js_slices import shipped_function

_NEW_FILES = (
    "src/coordinare/services/run_viewer.py",
    "src/coordinare/dashboard/routers/runs.py",
    "src/coordinare/dashboard/dashboard_static/runs.js",
)
_REPO = Path(__file__).resolve().parents[2]


def test_runs_checks_under_node(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "runs.js"
    js_path.write_text(
        "\n\n".join([shipped_function("helpers", "esc"), _DASHBOARD_JS_SOURCES["runs"]]),
        encoding="utf-8",
    )
    checks = _REPO / "tests" / "js" / "runs_checks.js"
    result = subprocess.run(
        [node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 60, f"only {summary.group(1)} checks ran:\n{report}"


def test_runs_js_loads_before_the_application_script() -> None:
    order = list(_DASHBOARD_JS_ORDER)
    assert order.index("runs") < order.index("app")


def test_the_page_stays_under_its_size_budget() -> None:
    assert len(_DASHBOARD_HTML.encode("utf-8")) <= DASHBOARD_HTML_BUDGET_BYTES


def test_the_page_has_the_viewer_section_and_the_router_knows_its_path() -> None:
    assert 'id="runs-page"' in _DASHBOARD_HTML
    assert 'id="runs-scroll"' in _DASHBOARD_HTML
    assert "isRunsPath(path)" in _DASHBOARD_JS_SOURCES["app"]
    assert "'runs-page'" in _DASHBOARD_JS_SOURCES["app"]


def test_the_card_detail_and_activity_rows_link_into_the_viewer() -> None:
    assert "hydrateCardRuns(detailContent)" in _DASHBOARD_JS_SOURCES["app"]
    assert "hydrateCardRuns(content)" in _DASHBOARD_JS_SOURCES["performers"]
    assert "runEntryLinkHtml(e)" in (_REPO / "src/coordinare/dashboard/templates/activity_streams.js").read_text(
        encoding="utf-8",
    )


def test_the_new_files_use_no_em_or_en_dashes() -> None:
    dashes = ("\u2014", "\u2013")
    for relative in (*_NEW_FILES, "tests/js/runs_checks.js", "specs/133-transcript-viewer/spec.md",
                     "specs/133-transcript-viewer/plan.md", "specs/133-transcript-viewer/tasks.md"):
        text = (_REPO / relative).read_text(encoding="utf-8")
        for dash in dashes:
            assert dash not in text, f"{relative} contains a dash ({dash!r})"


def test_a_tool_call_is_named_by_its_command_and_a_result_by_its_exit_code() -> None:
    assert run_viewer._tool_arg({"command": "pytest -q tests/"}) == "pytest -q tests/"
    assert run_viewer._tool_arg({"file_path": "a/b.py"}) == "a/b.py"
    assert run_viewer._tool_arg("ls   -la") == "ls -la"
    assert run_viewer._tool_arg({"other": 1}) == ""
    long = "x" * (run_viewer.TOOL_ARG_CHARS + 50)
    assert run_viewer._tool_arg({"command": long}).endswith("...")
    assert run_viewer._exit_code({"exit_code": 2}) == 2
    assert run_viewer._exit_code({"returncode": 0}) == 0
    assert run_viewer._exit_code({"exit_code": True}) is None
    assert run_viewer._exit_code("tests failed, exit code: 3") == 3
    assert run_viewer._exit_code("no status here") is None

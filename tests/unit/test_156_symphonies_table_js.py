"""156: the Symphonies table, checked as shipped.

The pure row and status functions run under node (tests/js/symphonies_table_156_checks.js). The
static checks read the shipped script and page: the table's columns, the header copy, no dashes
in the table's text, the page budget, and that the table reads only the scoped or per-row data.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import DASHBOARD_HTML_BUDGET_BYTES

_REPO = Path(__file__).resolve().parents[2]
_APP = _DASHBOARD_JS_SOURCES["app"]
_SYMPHONIES = _DASHBOARD_JS_SOURCES["symphonies"]
_EM_DASH = "\u2014"
_EN_DASH = "\u2013"


def _render_symphonies_page() -> str:
    match = re.search(r"function renderSymphoniesPage\(s\) \{.*?\n\}\n", _APP, re.DOTALL)
    assert match is not None
    return match.group(0)


def test_the_symphonies_table_has_the_columns_the_issue_asks_for() -> None:
    page = _render_symphonies_page()
    for header in (
        "Name",
        "Status",
        "GitHub Project",
        "Repository",
        "Env",
        "Cards active",
        "Cards queued",
        "Spend this week",
        "Checks run",
        "Failed checks",
        "Last checked",
    ):
        assert f">{header}</th>" in page, header
    assert "Priority" not in page
    assert "'Errors'" not in page and ">Errors<" not in page


def test_the_table_builds_each_row_with_the_row_function() -> None:
    page = _render_symphonies_page()
    assert "symphonyTableRowHtml(sym, choice)" in page
    assert "currentSymphonyChoice(s)" in page


def test_a_click_on_a_row_opens_that_symphonys_page_and_keeps_the_links_and_buttons() -> None:
    page = _render_symphonies_page()
    assert "tr.symphony-row" in page
    assert "e.target.closest('a,button')" in page
    assert "navigate(e, tr.getAttribute('data-href'))" in page


def test_no_dash_is_written_into_the_table_or_its_row_function() -> None:
    row_function = re.search(r"function symphonyTableRowHtml\(.*?\n\}\n", _SYMPHONIES, re.DOTALL)
    assert row_function is not None
    table_text = _render_symphonies_page() + row_function.group(0)
    assert _EM_DASH not in table_text and _EN_DASH not in table_text


def test_the_table_placeholders_are_words_not_dashes() -> None:
    row_function = re.search(r"function symphonyTableRowHtml\(.*?\n\}\n", _SYMPHONIES, re.DOTALL)
    assert row_function is not None
    assert "No project number" in row_function.group(0)
    assert "No repository" in row_function.group(0)
    assert "Not known" in _SYMPHONIES


def test_the_table_reads_spend_and_env_from_the_row_not_the_whole_snapshot() -> None:
    row_function = re.search(r"function symphonyTableRowHtml\(.*?\n\}\n", _SYMPHONIES, re.DOTALL)
    assert row_function is not None
    body = row_function.group(0)
    assert "sym.spend_week" in body
    assert "token_usage" not in body
    assert "by_symphony" not in body


def test_the_dashboard_page_stays_under_its_size_budget() -> None:
    assert len(_DASHBOARD_HTML.encode("utf-8")) < DASHBOARD_HTML_BUDGET_BYTES


def test_the_symphonies_table_functions_pass_their_node_checks(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    bundle = tmp_path / "symphonies_table_156_bundle.js"
    bundle.write_text(
        "\n\n".join(_DASHBOARD_JS_SOURCES[name] for name in ("helpers", "usage", "symphonies")),
        encoding="utf-8",
    )
    checks = _REPO / "tests" / "js" / "symphonies_table_156_checks.js"
    result = subprocess.run(
        [node, str(checks), str(bundle)], capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"symphonies table checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 20, f"only {summary.group(1)} checks ran:\n{report}"

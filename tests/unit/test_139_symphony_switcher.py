"""#139: "Symphony" is the one user-facing name, the header switcher replaces the
board subtitle and the board tabs, and "Board" and "swimlane" are gone from the UI.

The browser behaviour is in tests/e2e/test_139_symphony_switcher.py. This file
covers the server side (glossary, snapshot identity fields, spend by symphony),
the static wording, and the pure switcher functions under node.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare import vocabulary
from coordinare.config import CostTrackingConfig
from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import _DASHBOARD_JS_ORDER, DASHBOARD_HTML_BUDGET_BYTES
from coordinare.dashboard.store import DashboardStore
from coordinare.services.token_usage import TokenLedger, price_table_from_config, usage_block
from tests.unit.dashboard_js_slices import app_js, symphony_switcher_js

_REPO = Path(__file__).resolve().parents[2]
_APP = _DASHBOARD_JS_SOURCES["app"]
_PERFORMERS = _DASHBOARD_JS_SOURCES["performers"]
_SYMPHONIES = _DASHBOARD_JS_SOURCES["symphonies"]
_STATIC_TEXT = _DASHBOARD_HTML + "".join(_DASHBOARD_JS_SOURCES.values())

# --- the glossary --------------------------------------------------------------- #


def test_the_glossary_names_the_terms_the_switcher_uses() -> None:
    assert {"symphony", "github_project", "repository", "cards"} <= set(vocabulary.CONCEPTS)
    assert vocabulary.CONCEPTS["cards"].label == "Cards"
    assert vocabulary.CONCEPTS["github_project"].label == "GitHub Project"
    assert vocabulary.CONCEPTS["repository"].label == "Repository"


def test_the_glossary_no_longer_calls_a_symphony_a_board() -> None:
    for code in ("symphony", "check", "cards"):
        description = vocabulary.CONCEPTS[code].description
        assert not re.search(r"\bboard", description, re.IGNORECASE), code  # "dashboard" is fine
    assert "switcher" in vocabulary.CONCEPTS["symphony"].description


# --- server-side identity fields ------------------------------------------------- #


def _daemon(*, global_cfg: Any, symphonies: dict[str, Any]) -> Any:
    return SimpleNamespace(state={
        "config": global_cfg,
        "symphony_configs": symphonies,
        "symphony_states": {},
        "env_cache": {},
    })


def test_a_symphony_names_its_owner_and_repository_from_the_global_config() -> None:
    global_cfg = SimpleNamespace(github_org="ViviDynamics", project_name="")
    daemon = _daemon(global_cfg=global_cfg, symphonies={
        "conductor-e2e": SimpleNamespace(github_project_number=3, overrides=None),
    })
    [entry] = DashboardStore._build_symphonies_data(daemon)
    assert entry["github_org"] == "ViviDynamics"
    assert entry["repo"] == "conductor-e2e"  # no project_name anywhere: the symphony's name
    assert entry["github_project_number"] == 3


def test_an_override_wins_over_the_global_repository_and_owner() -> None:
    global_cfg = SimpleNamespace(github_org="acme", project_name="global-repo")
    daemon = _daemon(global_cfg=global_cfg, symphonies={
        "one": SimpleNamespace(github_project_number=1, overrides={"project_name": "sample", "github_org": "other"}),
        "two": SimpleNamespace(github_project_number=2, overrides=None),
    })
    one, two = DashboardStore._build_symphonies_data(daemon)
    assert (one["github_org"], one["repo"]) == ("other", "sample")
    assert (two["github_org"], two["repo"]) == ("acme", "global-repo")


def test_unknown_owner_or_repository_is_empty_not_a_placeholder() -> None:
    daemon = _daemon(global_cfg=None, symphonies={"lonely": SimpleNamespace(github_project_number=7)})
    [entry] = DashboardStore._build_symphonies_data(daemon)
    assert entry["github_org"] == ""
    assert entry["repo"] == "lonely"


# --- spend by symphony ----------------------------------------------------------- #


def _price_table() -> Any:
    config = CostTrackingConfig.model_validate({
        "pricing_as_of": "2026-10-01",
        "prices": [{
            "models": ["claude-opus-4-5"], "input_per_million": 15.0, "output_per_million": 75.0,
            "cache_read_per_million": 1.5, "cache_write_per_million": 18.75,
        }],
    })
    return price_table_from_config(config)


def test_the_usage_block_gives_each_named_symphony_its_own_figures() -> None:
    ledger = TokenLedger()
    ledger.observe("run-a", symphony="alpha", card="C1", stage="implementing", records=[{
        "ts": "2026-10-14T10:00:00+00:00", "seq": 1, "type": "usage", "scope": "message",
        "usage": {"input_tokens": 10, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "claude-opus-4-5"},
    }])
    block = usage_block(ledger, _price_table(), today=date(2026, 10, 14), symphonies=["alpha", "beta"])
    assert block["by_symphony"]["alpha"]["today"]["tokens"]["input"] == 10
    assert block["by_symphony"]["beta"]["today"]["tokens"]["input"] == 0
    assert block["by_symphony"]["beta"]["week"]["tokens"]["input"] == 0
    assert block["today"]["tokens"]["input"] == 10  # the deployment total is still there


def test_the_usage_block_with_no_symphonies_has_an_empty_breakdown() -> None:
    block = usage_block(TokenLedger(), _price_table(), today=date(2026, 10, 14))
    assert block["by_symphony"] == {}


# --- the shipped page -------------------------------------------------------------- #


def test_the_header_is_the_switcher_and_the_board_tabs_are_gone() -> None:
    assert '<span id="project-link" class="symphony-switcher"' in _DASHBOARD_HTML
    assert 'id="swimlane-tabs"' not in _DASHBOARD_HTML
    assert "selectSwimlaneTab" not in app_js()
    assert "_swimlaneTabExplicit" not in _PERFORMERS
    assert "swimlane-tab" not in _PERFORMERS  # the tab buttons are no longer rendered


def test_no_swimlane_tab_markup_or_style_remains_anywhere() -> None:
    # The board tabs are gone (#139), and the Configuration tabs that borrowed their
    # classes now have their own names. Nothing still calls the tab strip swimlane.
    shipped = _STATIC_TEXT + _DASHBOARD_JS_SOURCES["config"]
    assert "swimlane-tab" not in shipped
    assert 'id="swimlane-tabs"' not in shipped


def test_the_panel_is_called_cards_and_keeps_its_id() -> None:
    # 154: the heading carries the board's counts beside its name (cards-summary).
    assert '<h2>Cards <span id="cards-summary"' in _DASHBOARD_HTML
    assert 'id="swimlane-section"' in _DASHBOARD_HTML  # ids stay: tests and the MCP tools depend on them
    assert "&#8592; Cards</button>" in _DASHBOARD_HTML


@pytest.mark.parametrize("phrase", [
    "Board total", "Board item", "Waiting for board snapshot", "<h2>Board</h2>", "&#8592; Board<",
    "' Board'", "GitHub Project board", "in this board", "off-board", ">Project</th>",
])
def test_no_user_facing_board_wording_remains(phrase: str) -> None:
    assert phrase not in _STATIC_TEXT, phrase


def test_the_symphonies_table_names_the_github_project_and_repository() -> None:
    assert ">GitHub Project</th>" in app_js()
    assert ">Repository</th>" in app_js()


def test_the_symphony_script_loads_before_the_application_script() -> None:
    assert _DASHBOARD_JS_ORDER.index("symphonies") < _DASHBOARD_JS_ORDER.index("app")
    assert "symphonies" in _DASHBOARD_JS_SOURCES


def test_the_page_stays_under_its_budget() -> None:
    assert len(_DASHBOARD_HTML.encode("utf-8")) < DASHBOARD_HTML_BUDGET_BYTES


def test_the_mcp_state_tool_no_longer_calls_the_snapshot_a_board() -> None:
    source = (_REPO / "src" / "coordinare" / "mcp" / "tools" / "state_tools.py").read_text(encoding="utf-8")
    assert not re.search(r"\bboard state", source, re.IGNORECASE)  # "dashboard state" is fine


# --- the switcher's pure functions under node ---------------------------------------- #


def test_the_switcher_functions_pass_their_node_checks(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "symphony_switcher.js"
    js_path.write_text(symphony_switcher_js(), encoding="utf-8")
    checks = _REPO / "tests" / "js" / "symphony_switcher_checks.js"
    result = subprocess.run([node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"symphony switcher checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 50, f"only {summary.group(1)} checks ran:\n{report}"


def test_the_app_script_renders_every_view_through_the_switcher() -> None:
    # The listener and the router both go through the switcher, so no view shows the unscoped snapshot.
    assert "scopedView(_lastState)" in _APP
    assert "renderSymphonySwitcher();" in _APP

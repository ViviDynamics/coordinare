"""#155: the board's Done (last 24h) lane, the PR checks icon and the inactive symphony.

The data is read from the lifecycle log (the strip each card carries in card_pipeline) and the
symphony entries. The browser behaviour is in tests/e2e/test_155_board.py. This file covers the
server side (the strip's checks state and merge time, the never-checked symphony entry), the
static wording, and the pure board functions under node.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import DASHBOARD_HTML_BUDGET_BYTES
from coordinare.dashboard.card_pipeline import card_pipeline_block
from coordinare.dashboard.store import DashboardStore
from coordinare.services.card_lifecycle import CardLifecycleLog
from coordinare.services.card_timeline import (
    CHECKS_GREEN,
    CHECKS_RED,
    CHECKS_RUNNING,
    LiveStage,
    checks_state,
    merged_at,
    pipeline_strip,
)

_REPO = Path(__file__).resolve().parents[2]
_APP = _DASHBOARD_JS_SOURCES["app"]
_PERFORMERS = _DASHBOARD_JS_SOURCES["performers"]
_SYMPHONIES = _DASHBOARD_JS_SOURCES["symphonies"]
_DONE_AT = "2026-10-14T10:00:00.123456+00:00"


def _ci(result: str, at: str = "2026-10-14T09:00:00+00:00") -> dict[str, Any]:
    return {"at": at, "kind": "ci_result", "stage": "implementing", "detail": {"result": result, "head": "abc"}}


def _merged(at: str = _DONE_AT) -> dict[str, Any]:
    return {"at": at, "kind": "merged", "stage": "closing_review", "detail": {}}


# --- the checks state -------------------------------------------------------------- #


@pytest.mark.parametrize(("result", "state"), [
    ("FORWARD", CHECKS_GREEN),
    ("BOUNCE", CHECKS_RED),
    ("HOLD", CHECKS_RUNNING),
])
def test_a_gate_result_maps_to_the_checks_state_it_stands_for(result: str, state: str) -> None:
    assert checks_state([_ci(result)]) == state


def test_the_latest_gate_result_is_the_checks_state() -> None:
    events = [_ci("HOLD", "2026-10-14T09:00:00+00:00"), _ci("FORWARD", "2026-10-14T09:30:00+00:00")]
    assert checks_state(events) == CHECKS_GREEN


def test_a_card_no_gate_has_judged_has_no_checks_state() -> None:
    assert checks_state([]) is None
    assert checks_state([{"at": "t", "kind": "dispatched", "stage": "implementing", "detail": {}}]) is None


def test_an_unknown_gate_result_is_no_checks_state_rather_than_a_guess() -> None:
    assert checks_state([_ci("SOMETHING_NEW")]) is None


# --- the Done lane's data: the merge time on the strip --------------------------- #


def test_a_merged_card_carries_its_merge_time_on_the_strip() -> None:
    strip = pipeline_strip([_ci("FORWARD"), _merged()], LiveStage())
    assert strip["merged_at"] == _DONE_AT
    assert strip["checks"] == CHECKS_GREEN


def test_a_card_not_merged_here_has_no_merge_time() -> None:
    assert merged_at([_ci("FORWARD")]) is None
    assert pipeline_strip([], LiveStage())["merged_at"] is None


def _daemon_with_log(log: CardLifecycleLog) -> Any:
    return SimpleNamespace(state={"card_lifecycle": log, "symphony_states": {}, "lifecycle_sequence": []})


def test_the_snapshot_block_carries_the_checks_state_and_merge_time_per_card() -> None:
    log = CardLifecycleLog()
    log.record("alpha", "c1", "dispatched", stage="implementing", at="2026-10-14T08:00:00+00:00")
    log.record("alpha", "c1", "ci_result", stage="implementing", at="2026-10-14T09:00:00+00:00",
               detail={"result": "BOUNCE", "head": "abc"}, key="abc:BOUNCE")
    log.record("alpha", "c2", "merged", stage="closing_review", at=_DONE_AT, key="merged")
    block = card_pipeline_block(_daemon_with_log(log))
    assert block["alpha"]["c1"]["checks"] == CHECKS_RED
    assert block["alpha"]["c1"]["merged_at"] is None
    assert block["alpha"]["c2"]["merged_at"] == _DONE_AT
    assert block["alpha"]["c2"]["checks"] is None


# --- the inactive symphony: the server's entry for one that was never checked ---- #


def test_a_symphony_never_checked_has_no_poll_no_cycles_and_no_state() -> None:
    daemon = SimpleNamespace(state={
        "config": SimpleNamespace(github_org="ViviDynamics", project_name=""),
        "symphony_configs": {"placeholder": SimpleNamespace(github_project_number=None, overrides=None)},
        "symphony_states": {},
        "env_cache": {},
    })
    [entry] = DashboardStore._build_symphonies_data(daemon)
    assert entry["cycle_count"] == 0
    assert entry["last_poll_at"] is None
    assert entry["state"] is None


# --- static wording and layout ------------------------------------------------------ #


def test_the_page_title_no_longer_carries_a_dash() -> None:
    assert "\u2014 Coordinare" not in _APP
    assert "Dashboard | Coordinare" in _APP


def test_the_header_subtitle_is_the_switcher_with_no_leading_dash() -> None:
    header = re.search(r'<header class="workspace-header">.*?</header>', _DASHBOARD_HTML, re.DOTALL)
    assert header is not None
    assert "\u2014" not in header.group(0)
    assert "workspace-title" in header.group(0)


def test_the_dashboard_page_stays_under_its_size_budget() -> None:
    assert len(_DASHBOARD_HTML.encode("utf-8")) < DASHBOARD_HTML_BUDGET_BYTES


def test_the_done_lane_and_the_checks_icon_are_in_the_board_code() -> None:
    assert "doneLaneHtml(doneLaneEntries(" in _PERFORMERS
    assert "checksIconHtml(checksState)" in _PERFORMERS
    assert "symphonyIsInactive" in _SYMPHONIES


# --- the pure board functions under node ---------------------------------------------- #


def test_the_board_functions_pass_their_node_checks(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    bundle = tmp_path / "board_155_bundle.js"
    bundle.write_text("\n\n".join(
        _DASHBOARD_JS_SOURCES[name] for name in ("helpers", "performers", "waiting", "timeline", "symphonies")
    ), encoding="utf-8")
    checks = _REPO / "tests" / "js" / "board_155_checks.js"
    result = subprocess.run([node, str(checks), str(bundle)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"board checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 18, f"only {summary.group(1)} checks ran:\n{report}"

"""#139 second review (PR #210): every view takes the scoped snapshot, in a real browser.

One test per review item. Each fails on the commit it was written against.
"""
from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_139_review import _busy_alpha_snapshot
from tests.e2e.test_139_symphony_switcher import _connected, _snapshot, _two_symphonies
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session

_BETA_URL = "/?symphony=beta-sym"


def _alpha_quiet_snapshot() -> dict[str, Any]:
    """Alpha has a live session that has been silent since long before the quiet threshold."""
    session = _active_session("PVTI_A1", "Alpha card", "implementing", phase="monitoring_performer")
    session["session_id"] = "sess-alpha"
    session["agent_dispatch_at"] = "2026-10-14T00:00:00+00:00"
    return _snapshot(_two_symphonies(), active_sessions=[session], activity_quiet_threshold_seconds=1)


# --- item 1: the active performer tiles follow the scope on a quiet tick ----------- #


@pytest.mark.e2e
def test_a_quiet_tick_under_a_selected_symphony_shows_only_its_own_tiles(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _connected(page, live_server_url + _BETA_URL)
    store.broadcaster.broadcast(_alpha_quiet_snapshot())
    expect(page.locator("#symphony-select")).to_have_value("beta-sym", timeout=_WAIT_LIVE)
    page.wait_for_timeout(3000)  # the quiet tick runs about once a second

    expect(page.locator("#active-performer-tiles .ap-pill-card[title='Card PVTI_A1']")).to_have_count(0)


# --- item 2: the history page agrees with the dashboard about cycle history ------- #


@pytest.mark.e2e
def test_the_history_page_does_not_show_another_symphonys_checks(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _connected(page, live_server_url + "/history?symphony=beta-sym")
    checks = [{"phase": "merging", "outcome": "success", "timestamp": "2026-10-14T06:00:00+00:00",
               "duration_seconds": 2.0}]
    store.broadcaster.broadcast(_snapshot(_two_symphonies(), cycle_history=checks))

    expect(page.locator("#history-page-section")).to_contain_text(
        "Recent checks are shown for all symphonies only.", timeout=_WAIT_LIVE,
    )
    expect(page.locator("#history-page-section")).not_to_contain_text("Merging")


# --- item 3: the flow chart draws the selected symphony's phase after a theme change ---- #


@pytest.mark.e2e
def test_a_theme_change_redraws_the_flow_chart_for_the_selected_symphony(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _connected(page, live_server_url + _BETA_URL)
    store.broadcaster.broadcast(_busy_alpha_snapshot())
    expect(page.locator("#symphony-select")).to_have_value("beta-sym", timeout=_WAIT_LIVE)
    page.wait_for_timeout(500)
    page.evaluate("""() => { window.__phases = []; const orig = window.updateFlowChart;
        window.updateFlowChart = function (p) { window.__phases.push(p); return orig.apply(this, arguments); }; }""")

    page.locator("[data-appearance='dark']").click()
    page.wait_for_timeout(500)

    phases = page.evaluate("window.__phases")
    assert phases, "the theme change did not redraw the flow chart"
    assert "monitoring_pr" not in phases, f"the flow chart drew alpha's phase under beta: {phases}"


# --- item 4: a blocked or failed rebase job stays visible under every scope --------- #


def _rebase_round(*jobs: dict[str, Any]) -> dict[str, Any]:
    return {"trigger_pr_number": 1, "trigger_sha": "abc123", "timestamp": "2026-10-14T06:00:00+00:00",
            "jobs": list(jobs)}


def _job(card_id: str, outcome: str) -> dict[str, Any]:
    return {"card_id": card_id, "branch": f"coordinare/{card_id}", "pr_number": 2, "pre_rebase_sha": "",
            "post_rebase_sha": "", "target_main_sha": "", "outcome": outcome, "conflicted_files": [],
            "conflict_preview": "", "duration_seconds": 0.0}


@pytest.mark.e2e
def test_a_blocked_rebase_job_stays_visible_under_a_selected_symphony(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _connected(page, live_server_url + _BETA_URL)
    store.broadcaster.broadcast(_snapshot(_two_symphonies(), last_rebase_round=_rebase_round(
        _job("PVTI_A1", "blocked"),   # alpha's, and it needs a person
        _job("PVTI_B1", "clean"),     # beta's, and healthy: informational only
    )))
    expect(page.locator("#symphony-select")).to_have_value("beta-sym", timeout=_WAIT_LIVE)

    expect(page.locator("#rebase-status-card")).to_be_visible()
    expect(page.locator("#rebase-status-list")).to_contain_text("blocked")
    expect(page.locator("#rebase-status-list")).to_contain_text("alpha-sym")
    expect(page.locator("#rebase-status-list")).not_to_contain_text("clean")


# --- item 5: the feed follows the scope when a snapshot arrives after its rows ------ #


@pytest.mark.e2e
def test_feed_rows_from_another_symphony_do_not_linger_when_the_snapshot_arrives_late(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    store.activity_log.record(
        activity_type="stage_change", card_id="PVTI_A1", card_number=1,
        card_title="Alpha card", stage="implementing", text="alpha early",
    )
    _connected(page, live_server_url + _BETA_URL)
    expect(page.locator("#activity-feed")).to_contain_text("alpha early", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(_snapshot(_two_symphonies()))

    expect(page.locator("#symphony-select")).to_have_value("beta-sym", timeout=_WAIT_LIVE)
    expect(page.locator("#activity-feed")).not_to_contain_text("alpha early")


# --- item 6: the Symphonies page says it lists every symphony ------------------------ #


@pytest.mark.e2e
def test_the_symphonies_page_says_it_lists_every_symphony(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _connected(page, live_server_url + "/symphonies?symphony=beta-sym")
    store.broadcaster.broadcast(_snapshot(_two_symphonies()))

    expect(page.locator("#symphonies-scope-note")).to_contain_text(
        "Lists every symphony, whatever the switcher shows.", timeout=_WAIT_LIVE,
    )
    expect(page.locator("#symphonies-page-section")).to_contain_text("alpha-sym")


# --- item 7: the unclaimed-card label names the GitHub Project, not a board ---------- #


@pytest.mark.e2e
def test_an_unclaimed_item_is_labelled_without_the_word_board(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    question = {"card_id": "PVTI_LOST", "card_number": 99, "card_title": "Lost card", "stage": "implementing",
                "text": "Which account owns this?", "asked_at": "2026-10-14T09:00:00+00:00", "issue_url": None}
    _connected(page, live_server_url + _BETA_URL)
    store.broadcaster.broadcast(_snapshot(_two_symphonies(), open_questions=[question]))

    expect(page.locator("#attention-list")).to_contain_text(
        "not on any symphony's GitHub Project", timeout=_WAIT_LIVE,
    )
    attention = page.locator("#attention-list").inner_text()
    assert not re.search(r"board", attention, re.IGNORECASE), attention


# --- item 9: the Performers page says its busy counts cover every symphony ---------- #


@pytest.mark.e2e
def test_the_performers_page_labels_busy_counts_as_shared(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _connected(page, live_server_url + "/performers?symphony=beta-sym")
    store.broadcaster.broadcast(_snapshot(_two_symphonies()))

    expect(page.locator("#capacity-scope-note")).to_contain_text(
        "busy counts are shared by all symphonies", timeout=_WAIT_LIVE,
    )


# --- item 11: the dependency note says what is true when the panel is empty ---------- #


@pytest.mark.e2e
def test_the_dependency_note_says_blockers_are_shown_for_all_symphonies(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _connected(page, live_server_url + _BETA_URL)
    store.broadcaster.broadcast(_snapshot(_two_symphonies(), blocked_by_dependencies=[
        {"issue_number": 5, "title": "Blocking issue", "column": "IN_PROGRESS", "issue_url": None},
    ]))

    expect(page.locator("#dependency-blockers-list")).to_contain_text(
        "Dependency blockers are shown for all symphonies only.", timeout=_WAIT_LIVE,
    )
    expect(page.locator("#dependency-blockers-list")).not_to_contain_text("Blocking issue")

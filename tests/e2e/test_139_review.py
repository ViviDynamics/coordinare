"""#139 review (PR #210): the switcher's scope in a real browser.

Each test is one review item: a symphony's own figures under a selected
symphony (1), an unclaimed item that stays visible (2), in-app navigation that
keeps the choice (4), and a feed card filter that does not survive a switch (5).
"""
from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_139_symphony_switcher import _open, _snapshot, _two_symphonies
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session


def _busy_alpha_snapshot(**overrides: Any) -> dict[str, Any]:
    """Alpha is waiting on review with 4,242 tokens and its own check and error counts.

    Beta has no live session. The conductor's own fields still describe alpha, because
    the daemon processed alpha last. That is the leak under test.
    """
    symphonies = _two_symphonies()
    symphonies[0]["cycle_count"] = 7
    symphonies[0]["error_count"] = 1
    symphonies[1]["cycle_count"] = 2
    symphonies[1]["error_count"] = 0
    base: dict[str, Any] = {
        "phase": "monitoring_pr", "phase_label": "Waiting on review", "card_tokens_total": 4242,
        "agent_session_id": "sess-alpha", "cycles_completed": 7, "consecutive_error_count": 1,
        "active_sessions": [_active_session("PVTI_A1", "Alpha card", "reviewing", phase="monitoring_pr")],
    }
    base.update(overrides)
    return _snapshot(symphonies, **base)


# --- item 1: a selected symphony shows its own per-card and per-cycle values ---------- #


@pytest.mark.e2e
def test_under_a_selected_symphony_the_headline_and_tokens_are_not_another_symphonys(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url, store, _busy_alpha_snapshot())
    expect(page.locator("#phase")).to_contain_text("Waiting on review", timeout=_WAIT_LIVE)

    page.locator("#symphony-select").select_option("beta-sym")

    expect(page.locator("#phase")).not_to_contain_text("Waiting on review")
    expect(page.locator("#phase")).not_to_contain_text("Alpha card")
    expect(page.locator("#card-tokens-total")).not_to_contain_text("4,242")
    expect(page.locator("#card-tokens-total")).not_to_contain_text("4242")


@pytest.mark.e2e
def test_under_a_selected_symphony_the_checks_and_errors_are_its_own(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url, store, _busy_alpha_snapshot())
    expect(page.locator("#cycles-completed")).to_have_text("7", timeout=_WAIT_LIVE)

    page.locator("#symphony-select").select_option("beta-sym")

    expect(page.locator("#cycles-completed")).to_have_text("2")
    expect(page.locator("#error-count")).to_have_text("0")
    # The recent checks are not known per symphony, so the panel says so instead of showing them.
    expect(page.locator("#history-section")).to_contain_text("Shown for all symphonies only.")
    expect(page.locator("#health-scope-note")).to_have_text("Covers all symphonies.")


# --- item 2: an item on a card no symphony claims stays visible under every choice ----- #


@pytest.mark.e2e
def test_a_question_on_an_unclaimed_card_stays_visible_under_a_symphony(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    question = {"card_id": "PVTI_LOST", "card_number": 99, "card_title": "Orphan card", "stage": "implementing",
                "text": "Which account owns this?", "asked_at": "2026-10-14T09:00:00+00:00", "issue_url": None}
    _open(page, live_server_url, store, _snapshot(_two_symphonies(), open_questions=[question]))
    expect(page.locator("#attention-list")).to_contain_text("Which account owns this?", timeout=_WAIT_LIVE)

    page.locator("#symphony-select").select_option("beta-sym")

    expect(page.locator("#attention-list")).to_contain_text("Which account owns this?")
    expect(page.locator("#attention-list")).to_contain_text("not on a symphony's board")


# --- item 4: in-app navigation keeps the choice in the address bar ------------------- #


@pytest.mark.e2e
def test_navigation_keeps_the_symphony_in_the_address(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open(page, live_server_url, store, _snapshot(_two_symphonies()))
    page.locator("#symphony-select").select_option("beta-sym")

    page.locator("#navbar a.nav-link[href='/performers']").click()

    expect(page).to_have_url(re.compile(r"/performers\?symphony=beta-sym$"), timeout=_WAIT_SSE)
    expect(page.locator("#symphony-select")).to_have_value("beta-sym")


# --- item 5: a feed card filter set under another symphony is reset --------------------- #


@pytest.mark.e2e
def test_a_feed_card_filter_from_another_symphony_is_reset_on_switch(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url, store, _snapshot(_two_symphonies()))
    store.activity_log.record(
        activity_type="stage_change", card_id="PVTI_A1", card_number=1, card_title="Alpha card",
        stage="implementing", text="alpha lint",
    )
    store.activity_log.record(
        activity_type="stage_change", card_id="PVTI_B1", card_number=2, card_title="Beta card",
        stage="reviewing", text="beta lint",
    )
    expect(page.locator("#activity-feed")).to_contain_text("alpha lint", timeout=_WAIT_SSE)
    page.locator("#af-filter").select_option("PVTI_A1")

    page.locator("#symphony-select").select_option("beta-sym")

    expect(page.locator("#af-filter")).to_have_value("")
    expect(page.locator("#af-filter option[value='PVTI_A1']")).to_have_count(0)
    expect(page.locator("#activity-feed")).not_to_contain_text("alpha lint")
    expect(page.locator("#activity-feed")).to_contain_text("beta lint")

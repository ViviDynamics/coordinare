"""153: the needs-attention signal, in a real browser.

A card that needs a person shows a count in the tab title, a badge on the nav
and on the favicon, and a dismissible banner on the Dashboard. These tests run
the page against a live server and push snapshots through the same SSE stream
the daemon uses.
"""
from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Dialog, Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_CARD = "PVTI_153"
_ISSUE = "https://github.com/acme/app/issues/153"
_COUNT_TITLE = re.compile(r"^\(1\) Dashboard")
_NO_COUNT_TITLE = re.compile(r"^Dashboard ")


def _question(text: str = "Should we use OAuth2?", card_title: str = "Add login") -> dict[str, Any]:
    return {
        "card_id": _CARD, "card_number": 153, "card_title": card_title, "stage": "implementing",
        "text": text, "asked_at": "2026-10-13T09:00:00+00:00", "issue_url": _ISSUE,
    }


def _blocked(questions: list[dict[str, Any]], *, subsystem_down: bool = False) -> dict[str, Any]:
    subsystems = [{"name": "github", "status": "healthy", "required": True, "checked_at": "2026-10-13T09:01:00+00:00"}]
    if subsystem_down:
        subsystems = [{
            "name": "github", "status": "unavailable", "required": True,
            "checked_at": "2026-10-13T09:01:00+00:00", "details": "403 from API",
        }]
    return _full_snapshot(
        phase="blocked",
        phase_label="Blocked",
        active_sessions=[_active_session(_CARD, "Add login", "implementing", phase="blocked", issue_number=153, issue_url=_ISSUE)],
        open_questions=questions,
        subsystems=subsystems,
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
        role_utilization=[{"role": "implementing", "active": 1, "max": 2, "queued": 0}],
    )


def _idle() -> dict[str, Any]:
    return _full_snapshot(lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"])


def _broadcast(store: DashboardStore, snapshot: dict[str, Any]) -> None:
    store.broadcaster.broadcast(snapshot)


def _open_dashboard(page: Page, live_server_url: str) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)


@pytest.mark.e2e
def test_the_title_shows_the_count_and_clears_when_nothing_needs_a_person(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url)
    _broadcast(store, _blocked([_question()]))
    expect(page).to_have_title(_COUNT_TITLE, timeout=_WAIT_LIVE)

    _broadcast(store, _idle())
    expect(page).to_have_title(_NO_COUNT_TITLE, timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_count_survives_navigation_to_another_page_and_back(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url)
    _broadcast(store, _blocked([_question()]))
    expect(page).to_have_title(_COUNT_TITLE, timeout=_WAIT_LIVE)

    page.locator("#navbar a[href='/performers']").click()
    expect(page).to_have_title(re.compile(r"^\(1\) Performers"), timeout=_WAIT_LIVE)

    page.locator("#navbar a[href='/']").click()
    expect(page).to_have_title(_COUNT_TITLE, timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_nav_badge_and_the_favicon_show_the_count_and_clear(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url)
    _broadcast(store, _blocked([_question()]))
    dashboard_badge = page.locator("#navbar a[data-attention='dashboard'] .nav-badge")
    performers_badge = page.locator("#navbar a[data-attention='performers'] .nav-badge")
    expect(dashboard_badge).to_have_text("1", timeout=_WAIT_LIVE)
    expect(performers_badge).to_have_text("1")
    # The count is also in the link's accessible name, not only the badge.
    expect(page.locator("#navbar a[data-attention='dashboard']")).to_have_attribute("aria-label", "Dashboard, 1 needs attention")

    icon = page.locator("link[rel='icon']")
    expect(icon).to_have_attribute("type", "image/png", timeout=_WAIT_LIVE)

    _broadcast(store, _idle())
    expect(dashboard_badge).to_be_hidden(timeout=_WAIT_LIVE)
    expect(page.locator("#navbar a[data-attention='dashboard']")).to_have_attribute("aria-label", "Dashboard")
    expect(icon).to_have_attribute("type", "image/svg+xml", timeout=_WAIT_LIVE)
    expect(icon).to_have_attribute("href", "/static/coordinare-mark.svg")


@pytest.mark.e2e
def test_the_banner_lists_each_item_and_links_it(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_dashboard(page, live_server_url)
    _broadcast(store, _blocked([_question()], subsystem_down=True))

    banner = page.locator("#attention-panel")
    expect(banner).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#attention-count")).to_have_text("2 need attention")
    rows = page.locator("#attention-list li")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("Should we use OAuth2?")
    expect(rows.nth(0)).to_contain_text("Question")
    expect(rows.nth(1)).to_contain_text("Subsystem down")
    expect(rows.nth(0).get_by_role("link", name="#153 Add login")).to_have_attribute("href", _ISSUE)
    expect(rows.nth(1).get_by_role("link", name="github")).to_have_attribute("href", "#subsystems-section")


@pytest.mark.e2e
def test_a_run_item_links_to_the_run_detail_in_the_page(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_dashboard(page, live_server_url)
    snapshot = _blocked([])
    snapshot["active_sessions"][0]["issue_url"] = None
    _broadcast(store, snapshot)
    link = page.locator("#attention-list li").get_by_role("link", name="#153 Add login")
    expect(link).to_have_attribute("href", "/performers/implementing", timeout=_WAIT_LIVE)
    link.click()
    expect(page).to_have_url(re.compile(r"/performers/implementing$"))
    expect(page.locator("#performers-page-detail")).to_contain_text("Live Events", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_dismiss_hides_the_banner_but_not_the_count_and_a_new_item_brings_it_back(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url)
    _broadcast(store, _blocked([_question()]))
    banner = page.locator("#attention-panel")
    expect(banner).to_be_visible(timeout=_WAIT_LIVE)

    page.get_by_role("button", name="Dismiss this needs-attention list").click()
    expect(banner).to_be_hidden()
    # The count is the truth about the page, so dismissing leaves it alone.
    expect(page).to_have_title(_COUNT_TITLE)
    expect(page.locator("#navbar a[data-attention='dashboard'] .nav-badge")).to_have_text("1")
    expect(page.locator("link[rel='icon']")).to_have_attribute("type", "image/png")

    # A state update with the same item does not bring it back.
    _broadcast(store, _blocked([_question()]))
    expect(page.locator("#phase")).not_to_be_empty()
    expect(banner).to_be_hidden()

    # A new question does.
    _broadcast(store, _blocked([_question(), _question(text="Use Redis or Postgres?")]))
    expect(banner).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#attention-list")).to_contain_text("Use Redis or Postgres?")


@pytest.mark.e2e
def test_a_hostile_title_and_question_are_shown_as_text_and_run_no_script(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    dialogs: list[str] = []

    def record(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", record)
    _open_dashboard(page, live_server_url)
    hostile = "<img src=x onerror=alert(1)>"
    _broadcast(store, _blocked([_question(text=hostile, card_title=hostile)]))
    rows = page.locator("#attention-list li")
    expect(rows).to_have_count(1, timeout=_WAIT_LIVE)
    expect(rows.nth(0)).to_contain_text(hostile)
    expect(page.locator("#attention-list img")).to_have_count(0)
    assert dialogs == []

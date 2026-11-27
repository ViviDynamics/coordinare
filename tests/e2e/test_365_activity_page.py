"""#365 (a): History and Logs are the two views of one Activity page, in a real browser."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_dashboard_browser import _WAIT_NAV, _WAIT_SSE

_WAIT = 5_000


@pytest.mark.e2e
def test_the_activity_nav_item_opens_history_with_the_switch_on_history(page: Page, live_server_url: str) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    page.locator("#navbar a[href='/activity']").click()
    expect(page).to_have_url(f"{live_server_url}/activity/history", timeout=_WAIT)
    expect(page.locator("#history-page")).to_be_visible(timeout=_WAIT)
    expect(page.locator("#workspace-title")).to_have_text("Activity")
    expect(page).to_have_title("History | Activity | Coordinare")
    switch = page.locator("#activity-switch")
    expect(switch).to_be_visible()
    expect(switch.locator("a[aria-current='page']")).to_have_text("History")
    expect(page.locator("[data-activity-summary='history']")).to_be_visible()
    expect(page.locator("#navbar a[href='/activity']")).to_have_attribute("aria-current", "page")


@pytest.mark.e2e
def test_the_switch_moves_between_the_views_and_back_returns(page: Page, live_server_url: str) -> None:
    page.goto(f"{live_server_url}/activity/history")
    expect(page.locator("#history-page")).to_be_visible(timeout=_WAIT)
    page.locator("#activity-switch-logs").click()
    expect(page).to_have_url(f"{live_server_url}/activity/logs", timeout=_WAIT_NAV * 5)
    expect(page.locator("#logs-page")).to_be_visible()
    expect(page.locator("#history-page")).to_be_hidden()
    expect(page.locator("#activity-switch-logs")).to_have_attribute("aria-current", "page")
    expect(page.locator("#activity-switch-history")).not_to_have_attribute("aria-current", "page")
    expect(page.locator("[data-activity-summary='logs']")).to_be_visible()
    expect(page).to_have_title("Logs | Activity | Coordinare")
    page.go_back()
    expect(page).to_have_url(f"{live_server_url}/activity/history", timeout=_WAIT)
    expect(page.locator("#history-page")).to_be_visible()


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("old", "new"),
    [("/history?symphony=", "/activity/history?symphony="), ("/logs?level=error", "/activity/logs?level=error")],
)
def test_an_old_bookmark_lands_on_its_view_with_its_query(page: Page, live_server_url: str, old: str, new: str) -> None:
    page.goto(live_server_url + old)
    expect(page).to_have_url(live_server_url + new, timeout=_WAIT)
    view = "#logs-page" if "logs" in new else "#history-page"
    expect(page.locator(view)).to_be_visible(timeout=_WAIT)


@pytest.mark.e2e
def test_the_switch_is_hidden_on_other_pages(page: Page, live_server_url: str) -> None:
    page.goto(f"{live_server_url}/activity/logs")
    expect(page.locator("#activity-switch")).to_be_visible(timeout=_WAIT)
    page.locator("#navbar a[href='/performers']").click()
    expect(page.locator("#performers-page")).to_be_visible(timeout=_WAIT)
    expect(page.locator("#activity-switch")).to_be_hidden()

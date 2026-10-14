"""#146: the run detail is one click from the dashboard and has its own address."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Dialog, Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_CARD = "PVTI_146"
_ROLES = [
    {"role": "implementing", "active": 1, "max": 2, "queued": 0},
    {"role": "reviewing", "active": 0, "max": 1, "queued": 0},
]
_RUN_URL = re.compile(r"/performers/implementing$")


def _broadcast(store: DashboardStore, *, sessions: bool = True) -> None:
    active = [_active_session(_CARD, "Add login", "implementing", issue_number=146)] if sessions else []
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer" if sessions else "idle",
        phase_label="Watching an agent" if sessions else "Idle",
        active_sessions=active,
        role_utilization=_ROLES,
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    ))


def _open_dashboard(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)


@pytest.mark.e2e
def test_deep_link_loads_the_run_detail(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url + "/performers/implementing")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    detail = page.locator("#performers-page-detail")
    expect(detail).to_contain_text("Live Events", timeout=_WAIT_LIVE)
    expect(detail).to_contain_text("Process Logs (stderr)")
    expect(page.locator("#performers-page-detail-view")).to_be_visible()
    expect(page.locator("#performers-page-list-view")).to_be_hidden()
    expect(page.locator("#navbar a.nav-active")).to_have_text("Performers")


@pytest.mark.e2e
def test_a_row_is_focusable_and_enter_or_space_opens_the_run(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url + "/performers")
    expect(page.locator("#performers-page-tbody")).not_to_contain_text("Loading", timeout=_WAIT_SSE)
    _broadcast(store)
    row = page.locator('#performers-page tr[data-role="implementing"]')
    expect(row).to_have_attribute("tabindex", "0", timeout=_WAIT_LIVE)
    expect(row).to_have_attribute("role", "button")

    row.focus()
    page.keyboard.press("Enter")
    expect(page).to_have_url(_RUN_URL)
    expect(page.locator("#performers-page-detail")).to_contain_text("Live Events", timeout=_WAIT_LIVE)
    expect(page.locator("#performers-page-back")).to_be_focused()

    page.locator("#performers-page-back").click()
    expect(page.locator("#performers-page-list-view")).to_be_visible()
    row.focus()
    page.keyboard.press("Space")
    expect(page).to_have_url(_RUN_URL)


@pytest.mark.e2e
def test_a_row_click_opens_the_run_and_the_browser_back_button_returns(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url + "/performers")
    expect(page.locator("#performers-page-tbody")).not_to_contain_text("Loading", timeout=_WAIT_SSE)
    _broadcast(store)
    page.locator('#performers-page tr[data-role="implementing"]').click(timeout=_WAIT_LIVE)
    expect(page).to_have_url(_RUN_URL)
    expect(page.locator("#performers-page-detail")).to_contain_text("Live Events", timeout=_WAIT_LIVE)

    page.go_back()
    expect(page).to_have_url(re.compile(r"/performers$"))
    expect(page.locator("#performers-page-list-view")).to_be_visible()
    expect(page.locator("#performers-page-detail-view")).to_be_hidden()


@pytest.mark.e2e
def test_the_back_button_on_the_run_returns_to_the_list(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url + "/performers/implementing")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    expect(page.locator("#performers-page-detail")).to_contain_text("Live Events", timeout=_WAIT_LIVE)
    page.locator("#performers-page-back").click()
    expect(page).to_have_url(re.compile(r"/performers$"))
    expect(page.locator("#performers-page-list-view")).to_be_visible()
    # Focus returns to the row the run was opened from, not to the top of the page.
    expect(page.locator('#performers-page-tbody tr[data-role="implementing"]')).to_be_focused()


@pytest.mark.e2e
def test_the_active_performers_tile_links_to_the_run(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_dashboard(page, live_server_url, store)
    tile_link = page.locator("#active-performers .ap-tile").get_by_role("link", name="View run")
    expect(tile_link).to_have_attribute("href", "/performers/implementing", timeout=_WAIT_LIVE)
    tile_link.click()
    expect(page).to_have_url(_RUN_URL)
    expect(page.locator("#performers-page-detail")).to_contain_text("Live Events", timeout=_WAIT_LIVE)
    expect(page.locator("#dashboard-page")).to_be_hidden()


@pytest.mark.e2e
def test_the_board_card_links_to_the_run_without_opening_the_card(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url, store)
    card_link = page.locator("#swimlane-section .swimlane-card").get_by_role("link", name="View run")
    expect(card_link).to_be_visible(timeout=_WAIT_LIVE)
    card_link.click()
    expect(page).to_have_url(_RUN_URL)
    expect(page.locator("#performers-page-detail")).to_contain_text("Live Events", timeout=_WAIT_LIVE)
    expect(page.locator("#card-detail-view")).to_be_hidden()


@pytest.mark.e2e
def test_the_browser_back_button_from_the_tile_returns_to_the_dashboard(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url, store)
    page.locator("#active-performers .ap-tile").get_by_role("link", name="View run").click()
    expect(page).to_have_url(_RUN_URL)
    page.go_back()
    expect(page.locator("#dashboard-page")).to_be_visible()
    expect(page.locator("#performers-page")).to_be_hidden()


@pytest.mark.e2e
def test_an_unknown_role_shows_the_not_found_state(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url + "/performers/no-such-role")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    detail = page.locator("#performers-page-detail")
    expect(detail).to_contain_text("Run not found", timeout=_WAIT_LIVE)
    expect(detail).not_to_contain_text("Live Events")
    expect(page.locator("#performers-page-back")).to_be_visible()


@pytest.mark.e2e
def test_a_hostile_role_segment_is_shown_as_not_found_and_runs_no_script(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    dialogs: list[str] = []

    def record(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", record)
    page.goto(live_server_url + "/performers/%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    expect(page.locator("#performers-page-detail")).to_contain_text("Run not found", timeout=_WAIT_LIVE)
    expect(page.locator("#performers-page-detail img")).to_have_count(0)
    assert dialogs == []


@pytest.mark.e2e
def test_the_run_detail_has_its_own_title(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url + "/performers/implementing")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    expect(page).to_have_title("Run detail | Coordinare", timeout=_WAIT_SSE)

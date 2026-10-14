"""#141: one name and one id format for a run, in every view, checked in a real browser.

A run is named "run" and its first six id characters (run 299a83). The full id is in the title,
and the Copy button copies it. The container id appears only in the run detail's technical
section. Hostile ids reach the page as text. Runs in Chromium and Firefox (--browser firefox).
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_CARD = "PVTI_141"
_RUN = "299a8349-6f0e-4b1e-9d2a-0c1f2e3d4a5b"
_SHORT = "run 299a83"
_CONTAINER = "c0ffee1234567890abcdef"
_ROLES = [
    {"role": "implementing", "active": 1, "max": 2, "queued": 0},
    {"role": "reviewing", "active": 0, "max": 1, "queued": 0},
]
# A stage or session word in visible text is a label the issue retires.
_RETIRED = re.compile(r"\b(session|sessions|attempt|attempts)\b", re.IGNORECASE)


def _broadcast(
    store: DashboardStore, *, session_id: str = _RUN, container_id: str = _CONTAINER, full_log: bool = False,
) -> None:
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer",
        phase_label="Watching an agent",
        agent_session_id=session_id,
        agent_dispatch_at="2026-10-13T10:00:00+00:00",
        active_sessions=[_active_session(
            _CARD, "Name the run", "implementing",
            session_id=session_id, container_id=container_id, full_log_available=full_log, issue_number=141,
        )],
        role_utilization=_ROLES,
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    ))


def _open_dashboard(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)


def _open_run_detail(page: Page, live_server_url: str, store: DashboardStore, session_id: str = _RUN) -> None:
    page.goto(live_server_url + "/performers/implementing")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, session_id=session_id)
    expect(page.locator("#performers-page-detail")).to_contain_text("Live Events", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_current_run_is_named_once_on_the_tile_and_not_repeated_in_metrics(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    """154: the run that an active-run tile names is not repeated in the Metrics panel."""
    _open_dashboard(page, live_server_url, store)
    expect(page.locator("#active-performers .run-id")).to_have_text(_SHORT, timeout=_WAIT_LIVE)
    expect(page.locator("#agent-session")).to_be_hidden()
    assert page.locator("#agent-session-id .run-id").count() == 0


@pytest.mark.e2e
def test_the_active_performer_tile_names_its_run_by_the_short_id(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url, store)
    tile = page.locator("#active-performers .ap-tile")
    expect(tile.locator(".run-id")).to_have_text(_SHORT, timeout=_WAIT_LIVE)
    expect(tile.locator(".run-id")).to_have_attribute("title", _RUN)
    assert _RUN not in tile.inner_text()


@pytest.mark.e2e
def test_the_copy_button_copies_the_full_id_through_the_clipboard(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url, store)
    copy = page.locator("#active-performers .run-id-copy")
    expect(copy).to_be_visible(timeout=_WAIT_LIVE)
    page.evaluate(
        "() => { window.__copied = null; navigator.clipboard.writeText = (text) => {"
        " window.__copied = text; return Promise.resolve(); }; }",
    )
    copy.click()
    expect(copy).to_have_text("Copied")
    assert page.evaluate("() => window.__copied") == _RUN


@pytest.mark.e2e
def test_the_copy_button_falls_back_to_a_selected_field_without_the_clipboard(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url, store)
    copy = page.locator("#active-performers .run-id-copy")
    expect(copy).to_be_visible(timeout=_WAIT_LIVE)
    page.evaluate(
        "() => { Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });"
        " window.__copyCommand = null;"
        " document.execCommand = (command) => {"
        " window.__copyCommand = [command, (document.activeElement || {}).value || '']; return true; }; }",
    )
    copy.click()
    expect(copy).to_have_text("Copied")
    assert page.evaluate("() => window.__copyCommand") == ["copy", _RUN]
    # The temporary field is removed, and focus goes back to the Copy button.
    expect(copy).to_be_focused()


@pytest.mark.e2e
def test_the_run_detail_names_the_run_and_puts_the_container_in_the_technical_section(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_run_detail(page, live_server_url, store)
    detail = page.locator("#performers-page-detail")
    link = detail.locator("a.run-id-link")
    expect(link).to_have_text(_SHORT)
    expect(link).to_have_attribute("title", _RUN)
    expect(link).to_have_attribute("href", f"/runs/{_RUN}")
    expect(detail.locator(".detail-block", has_text="Technical")).to_contain_text(_CONTAINER[:12])
    visible = detail.inner_text()
    assert visible.count(_CONTAINER[:12]) == 1
    assert _CONTAINER not in visible
    assert _RUN not in visible


@pytest.mark.e2e
def test_the_container_id_is_shown_on_no_view_but_the_technical_section(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_dashboard(page, live_server_url, store)
    expect(page.locator("#active-performers .ap-tile")).to_be_visible(timeout=_WAIT_LIVE)
    assert _CONTAINER[:12] not in page.locator("body").inner_text()
    page.goto(live_server_url + "/performers")
    expect(page.locator("#performers-page-tbody")).not_to_contain_text("Loading", timeout=_WAIT_SSE)
    assert _CONTAINER[:12] not in page.locator("body").inner_text()


@pytest.mark.e2e
def test_the_card_detail_names_the_run_and_its_full_log_by_the_short_id(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, full_log=True)
    row = page.locator(f"#swimlane-section .swimlane-card[data-card-id='{_CARD}']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    row.click()
    detail = page.locator("#card-detail-content")
    expect(detail).to_contain_text("Live Log", timeout=_WAIT_LIVE)
    expect(detail.locator(".run-id").first).to_have_text(_SHORT)
    link = detail.locator("a.full-log-link")
    expect(link).to_have_text(_SHORT)
    expect(link).to_have_attribute("title", _RUN)
    expect(link).to_have_attribute("href", f"/api/runs/{_RUN}/log")


@pytest.mark.parametrize("path", ["/", "/performers", "/performers/implementing", "/history", "/glossary"])
@pytest.mark.e2e
def test_no_page_shows_a_session_or_attempt_label(
    page: Page, live_server_url: str, store: DashboardStore, path: str,
) -> None:
    page.goto(live_server_url + path)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    expect(page.locator("#navbar")).to_be_visible(timeout=_WAIT_SSE)
    visible = page.locator("body").inner_text()
    assert _RETIRED.search(visible) is None, _RETIRED.search(visible)


@pytest.mark.e2e
def test_a_hostile_run_id_reaches_the_page_as_text(page: Page, live_server_url: str, store: DashboardStore) -> None:
    hostile = '"><img src=x onerror=window.__pwned=1>'
    dialogs: list[str] = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    _open_run_detail(page, live_server_url, store, session_id=hostile)
    detail = page.locator("#performers-page-detail")
    expect(detail.locator(".run-id")).to_have_text('run "><img')
    expect(detail.locator("a.run-id-link")).to_have_count(0)
    assert detail.locator("img").count() == 0
    assert page.evaluate("() => window.__pwned === undefined")
    assert dialogs == []

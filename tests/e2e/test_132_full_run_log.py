"""#132: the card's Live Log offers the run's full log, one click away.

The Live Log keeps the tail. The "Open full log" link points at the run's log route
for the session's run id, and a hostile run id reaches the page as text, never markup.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_CARD = "PVTI_132"
_RUN = "run-132-e2e"
_LINK = "#card-detail-content a.full-log-link"


def _broadcast(store: DashboardStore, session_id: str | None) -> None:
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer",
        phase_label="Monitoring Performer",
        agent_dispatch_at="2026-10-13T10:00:00+00:00",
        active_sessions=[_active_session(_CARD, "Keep the full log", "implementing", session_id=session_id)],
    ))


def _open_card_detail(page: Page, live_server_url: str, store: DashboardStore, session_id: str | None) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, session_id)
    row = page.locator(f"#swimlane-section .swimlane-card[data-card-id='{_CARD}']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    row.click()
    expect(page.locator("#card-detail-content")).to_contain_text("Live Log", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_live_log_links_to_the_full_log_of_the_run(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_card_detail(page, live_server_url, store, _RUN)

    link = page.locator(_LINK)
    expect(link).to_have_text("Open full log ↗", timeout=_WAIT_LIVE)
    expect(link).to_have_attribute("href", f"/api/runs/{_RUN}/log")
    expect(link).to_have_attribute("target", "_blank")


@pytest.mark.e2e
def test_there_is_no_full_log_link_without_a_run(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_card_detail(page, live_server_url, store, None)

    expect(page.locator("#card-detail-content")).to_contain_text("No log entries yet.", timeout=_WAIT_LIVE)
    expect(page.locator(_LINK)).to_have_count(0)


@pytest.mark.e2e
def test_a_hostile_run_id_reaches_the_page_as_text_only(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    hostile = '"><img src=x onerror="window.__pwned=1">/../'
    _open_card_detail(page, live_server_url, store, hostile)

    link = page.locator(_LINK)
    expect(link).to_have_count(1, timeout=_WAIT_LIVE)
    expect(page.locator("#card-detail-content img")).to_have_count(0)
    assert page.evaluate("window.__pwned === undefined")
    href = link.get_attribute("href") or ""
    assert "/api/runs/" in href and href.endswith("/log")
    assert '"' not in href and "<" not in href and "/../" not in href


@pytest.mark.e2e
def test_the_full_log_route_is_served_by_the_dashboard(
    page: Page, live_server_url: str,
) -> None:
    # The e2e daemon keeps no run logs, so the route answers 404 for any run: the
    # point is that the route is mounted and answers in the dashboard app.
    response = page.request.get(f"{live_server_url}/api/runs/{_RUN}/log")

    assert response.status == 404
    assert "No full log" in response.text()

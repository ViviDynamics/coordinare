from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot


@pytest.mark.e2e
def test_idle_dashboard_explains_filter_and_offers_first_card(
    page: Page,
    live_server_url: str,
    store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            idle_reason="Idle: To Do cards are filtered by assignee. Check assignee_filter and include_unassigned.",
            ownership_hint="Assigned to first-launch-bot",
        ),
    )
    idle = page.locator(".ap-idle")
    expect(idle).to_contain_text("assignee_filter", timeout=_WAIT_LIVE)
    idle.get_by_text("Try a first card", exact=True).click()
    expect(idle).to_contain_text("FIRST_CARD.md")
    expect(idle).to_contain_text("pull request for a human to review")

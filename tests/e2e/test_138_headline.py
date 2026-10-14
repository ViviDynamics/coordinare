"""#138: the conductor headline's countdown keeps ticking between state updates."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot


@pytest.mark.e2e
def test_the_next_check_countdown_ticks_without_a_new_state_update(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    """154: the countdown moved from the headline to the freshness line; it still ticks between updates."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_contain_text("Idle", timeout=_WAIT_SSE)
    last_poll = page.evaluate("() => new Date(Date.now() - 2000).toISOString()")
    store.broadcaster.broadcast(_full_snapshot(poll_interval_seconds=60, last_poll_at=last_poll))
    expect(page.locator("#phase")).to_have_text("Idle: waiting for the next check", timeout=_WAIT_LIVE)
    freshness = page.locator("#connection-detail")
    expect(freshness).to_have_text(re.compile(r"next check in \d+s$"), timeout=_WAIT_LIVE)
    first = int(re.search(r"(\d+)s$", freshness.inner_text()).group(1))
    page.wait_for_function(
        "prev => { const m = document.getElementById('connection-detail').textContent.match(/(\\d+)s$/); return m && Number(m[1]) < prev; }",
        arg=first, timeout=_WAIT_LIVE,
    )

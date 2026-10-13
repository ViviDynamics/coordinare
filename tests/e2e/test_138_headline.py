"""#138: the conductor headline's countdown keeps ticking between state updates."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot


@pytest.mark.e2e
def test_idle_countdown_ticks_without_a_new_state_update(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_contain_text("Idle", timeout=_WAIT_SSE)
    last_poll = page.evaluate("() => new Date(Date.now() - 2000).toISOString()")
    store.broadcaster.broadcast(_full_snapshot(poll_interval_seconds=60, last_poll_at=last_poll))
    headline = page.locator("#phase")
    expect(headline).to_have_text(re.compile(r"^Idle: next check in \d+s$"), timeout=_WAIT_LIVE)
    first = int(re.search(r"(\d+)s$", headline.inner_text()).group(1))
    page.wait_for_function(
        "prev => { const m = document.getElementById('phase').textContent.match(/(\\d+)s$/); return m && Number(m[1]) < prev; }",
        arg=first, timeout=_WAIT_LIVE,
    )

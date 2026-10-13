"""#152: the header freshness indicator in a real browser."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot


@pytest.mark.e2e
def test_header_shows_age_and_next_check_and_keeps_ticking(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=_WAIT_SSE)
    last_poll = page.evaluate("() => new Date(Date.now() - 5000).toISOString()")
    store.broadcaster.broadcast(_full_snapshot(poll_interval_seconds=30, last_poll_at=last_poll))

    detail = page.locator("#connection-detail")
    expect(detail).to_have_text(re.compile(r"^ · updated \d+s ago · next check in \d+s$"), timeout=_WAIT_LIVE)
    first = detail.inner_text()
    page.wait_for_function(
        "prev => document.getElementById('connection-detail').textContent !== prev", arg=first, timeout=_WAIT_LIVE,
    )
    assert "conn-stale" not in (page.locator("#connection-status").get_attribute("class") or "")


@pytest.mark.e2e
def test_header_turns_amber_and_says_how_long_when_the_stream_goes_quiet(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=_WAIT_SSE)
    page.evaluate("() => { updateConnectionStatus('Reconnecting'); _afLastMsgAt = Date.now() - 125000; refreshFreshness(); }")
    expect(page.locator("#connection-detail")).to_have_text(" · no update for 2m: reconnecting…")
    expect(page.locator("#connection-status")).to_have_class(re.compile(r"conn-stale"))

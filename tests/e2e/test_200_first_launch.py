from __future__ import annotations

import asyncio

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot


@pytest.mark.e2e
@pytest.mark.parametrize('delay_snapshot', [False, True])
def test_idle_dashboard_explains_filter_and_offers_first_card(
    page: Page,
    live_server_url: str,
    store: DashboardStore,
    monkeypatch: pytest.MonkeyPatch,
    delay_snapshot: bool,
) -> None:
    if delay_snapshot:
        original = store.sse_stream

        async def delayed_stream(*args, **kwargs):
            # The static Idle label must not be mistaken for an SSE subscription.
            await asyncio.sleep(0.3)
            async for event in original(*args, **kwargs):
                yield event

        monkeypatch.setattr(store, 'sse_stream', delayed_stream)
    page.goto(live_server_url)
    page.wait_for_function("window._lastState !== null", timeout=_WAIT_SSE)
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

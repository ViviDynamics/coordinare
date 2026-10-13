"""#122: token and cost pending state in a real browser."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot


@pytest.mark.e2e
def test_running_stage_with_no_usage_shows_pending_then_the_real_count(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)

    running = {**_active_session("PVTI_122", "Running card", "implementing"),
               "agent_dispatch_at": "2026-10-13T12:00:00+00:00"}
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer", phase_label="Monitoring Performer",
        active_sessions=[running], cost_per_million_tokens=3.0,
    ))
    tokens = page.locator("#card-tokens-total")
    cost = page.locator("#card-cost-estimate")
    expect(tokens).to_have_text("—", timeout=_WAIT_LIVE)
    expect(tokens).to_have_attribute("title", "Reported when the stage ends")
    expect(cost).to_have_text("—")

    reported = {**running, "card_tokens_total": 2000000}
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer", phase_label="Monitoring Performer",
        active_sessions=[reported], cost_per_million_tokens=3.0,
    ))
    expect(tokens).to_have_text((2000000).__format__(","), timeout=_WAIT_LIVE)
    expect(cost).to_have_text("$6.00")

    store.broadcaster.broadcast(_full_snapshot(cost_per_million_tokens=3.0))
    expect(tokens).to_have_text("0", timeout=_WAIT_LIVE)
    expect(cost).to_have_text("$0.00")

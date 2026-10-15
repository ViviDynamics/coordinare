from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot


@pytest.mark.e2e
def test_export_loss_counts_are_visible_and_escaped(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_contain_text("Idle", timeout=_WAIT_SSE)
    snapshot = _full_snapshot(
        system_health={
            "overall": "unknown",
            "rows": [],
            "event_store": {
                "size_bytes": 10,
                "rows": 2,
                "oldest_ts": None,
                "dropped": 0,
                "pruned": 0,
                "writer_state": "idle",
                "export_dropped": 3,
                "export_failed": 4,
            },
        },
    )
    store.broadcaster.broadcast(snapshot)
    card = page.locator("#subsystems-section")
    line = card.locator(".sh-row").filter(has_text="Event store:")
    expect(line).to_contain_text("export 3 dropped, 4 failed sends", timeout=_WAIT_LIVE)
    card.locator("summary").click()
    expect(line).to_be_visible()
    snapshot["system_health"]["event_store"]["export_dropped"] = "<img src=x onerror=alert(1)>"
    store.broadcaster.broadcast(snapshot)
    expect(line).to_contain_text("<img src=x onerror=alert(1)>", timeout=_WAIT_LIVE)
    expect(line.locator("img")).to_have_count(0)

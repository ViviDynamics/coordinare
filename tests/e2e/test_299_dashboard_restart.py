from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _full_snapshot

WAIT_RECOVERY_MS = 45000
WAIT_BANNER_MS = 10000


@pytest.mark.e2e
def test_open_page_recovers_after_server_restart(
    page: Page, live_server_url: str, store: DashboardStore,
    live_server_lifecycle: dict[str, Any],
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=WAIT_RECOVERY_MS)
    store.broadcaster.broadcast(_full_snapshot(phase="idle", project_name="Deploy recovery test"))
    page.wait_for_function("window._lastState && window._lastState.project_name === 'Deploy recovery test'")
    page.evaluate("window.restartDocumentMarker = 'retained'")
    previous = page.evaluate("JSON.stringify(window._lastState)")
    previous_phase = page.locator("#phase").inner_text()
    live_server_lifecycle["stop"]()
    banner = page.locator("#disconnected-banner")
    expect(banner).to_be_visible(timeout=WAIT_BANNER_MS)
    expect(banner).to_contain_text("Reconnecting, Coordinare is restarting")
    assert page.evaluate("JSON.stringify(window._lastState)") == previous
    expect(page.locator("#phase")).to_have_text(previous_phase)
    live_server_lifecycle["start"](starting=True)
    response = page.request.get(live_server_url + "/api/force-poll")
    assert response.status == 503
    assert response.headers["retry-after"] == "2"
    expect(banner).to_be_visible()
    live_server_lifecycle["ready"]()
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=WAIT_RECOVERY_MS)
    expect(banner).to_be_hidden()
    assert page.evaluate("window.restartDocumentMarker") == "retained"
    assert page.evaluate("window._lastState.daemon_running") is True


@pytest.mark.e2e
def test_fresh_page_during_startup_serves_shell(
    page: Page, live_server_url: str, live_server_lifecycle: dict[str, Any],
) -> None:
    live_server_lifecycle["stop"]()
    live_server_lifecycle["start"](starting=True)
    response = page.goto(live_server_url)
    assert response is not None and response.status == 200
    expect(page.locator("#disconnected-banner")).to_contain_text("Coordinare is starting")
    live_server_lifecycle["ready"]()
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=WAIT_RECOVERY_MS)
    expect(page.locator("#disconnected-banner")).to_be_hidden()

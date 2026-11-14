"""Optional diagram delivery must never block the operational dashboard."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_stalled_diagram_cdn_does_not_block_dashboard_load(page: Page, live_server_url: str) -> None:
    pending = []
    page.route('https://cdn.jsdelivr.net/npm/mermaid@10/**', lambda route: pending.append(route))
    try:
        page.goto(live_server_url)
        expect(page.locator('#phase')).to_contain_text('Idle', timeout=3000)
        page.locator('#workflow-card summary').click()
        expect(page.locator('#flow-chart')).to_contain_text('Workflow diagram unavailable', timeout=7000)
        assert pending, 'the renderer request must actually have stalled'
    finally:
        for route in pending:
            route.abort()


@pytest.mark.e2e
def test_failed_diagram_delivery_keeps_live_dashboard_available(page: Page, live_server_url: str) -> None:
    page.route('https://cdn.jsdelivr.net/npm/mermaid@10/**', lambda route: route.abort())
    page.goto(live_server_url)
    expect(page.locator('#phase')).to_contain_text('Idle', timeout=3000)
    page.locator('#workflow-card summary').click()
    expect(page.locator('#flow-chart')).to_contain_text('Workflow diagram unavailable', timeout=3000)

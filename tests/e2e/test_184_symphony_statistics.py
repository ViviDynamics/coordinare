from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect

from coordinare.dashboard import DashboardStore
from coordinare.services.run_statistics import RunStatistics
from coordinare.services.stats_observability import live_health
from coordinare.services.stats_params import StatsQuery
from tests.e2e.test_156_symphonies_table import _row
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot
from tests.unit.test_182_run_statistics import NOW, TABLE, lifecycle, write_run

VIEWPORT_WIDTH = 375
VIEWPORT_HEIGHT = 812
CONTENT = "#sym-stats-content"


@pytest.fixture
def stats_responses(tmp_path: Path) -> dict[str, Any]:
    write_run(tmp_path, "run", "card", persona="reviewer")
    entry = _row("alpha", github_org="acme", repo="alpha", poll_interval_seconds=30)
    entry["max_concurrent_cards"] = 2
    entry["state"]["board_snapshot"] = {"TODO": ["queued"], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": ["card"]}
    entry["state"]["todo_waits"] = {"queued": {"reason": "queued"}}
    entry["state"]["todo_capacity"] = {"in_use": 0, "limit": 2}
    entry["state"]["board_titles"] = {"queued": "Queued card"}
    service = RunStatistics(tmp_path, table=TABLE)

    async def responses() -> dict[str, Any]:
        result = {}
        for window in ("24h", "7d", "30d"):
            for group in ("stage", "model", "symphony"):
                result[f"{window}:{group}"] = await service.query(
                    StatsQuery(window=window, group_by=group, symphony="alpha"),
                    visible=frozenset({"alpha"}), lifecycle=lifecycle(),
                    boards={"alpha": entry["state"]["board_snapshot"]}, now=NOW,
                )
                result[f"{window}:{group}"]["symphony_health"] = live_health({"alpha": entry}, frozenset({"alpha"}))
        return result

    return asyncio.run(responses())


def open_stats(page: Page, url: str, store: DashboardStore, bodies: dict[str, Any]) -> None:
    def respond(route: Route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        key = query.get("window", ["7d"])[0] + ":" + query.get("group_by", ["stage"])[0]
        route.fulfill(json=bodies[key])

    page.route("**/api/stats?*", respond)
    page.goto(url)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=_WAIT_SSE)
    entry = bodies["7d:stage"]["symphony_health"]["alpha"]
    store.broadcaster.broadcast(_full_snapshot(phase="idle", symphonies=[entry]))


@pytest.mark.e2e
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_read_only_stats_fit_mobile_in_both_themes(
    page: Page, live_server_url: str, store: DashboardStore, stats_responses: dict[str, Any], theme: str,
) -> None:
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    page.emulate_media(color_scheme=theme)
    open_stats(page, live_server_url + "/symphonies/alpha", store, stats_responses)
    content = page.locator(CONTENT)
    expect(content).to_contain_text("Throughput", timeout=_WAIT_LIVE)
    for label in ("Flow", "Human review wait", "Spend", "Reliability", "Env cache", "Recent runs"):
        expect(content).to_contain_text(label)
    expect(content.locator("button,input,form")).to_have_count(0)
    assert content.locator('svg[role="img"] title').count() > 0
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect(content.locator('a[href="/cards/alpha/card"]').first).to_be_visible()
    expect(content.locator('a[href="/runs/alpha/card/run"]')).to_be_visible()
    expect(content.locator('a[href="/history?h.symphony=alpha"]')).to_be_visible()
    page.locator("#sym-stats-window").select_option("24h")
    expect(content.locator('[title*="Window: 24h"]').first).to_be_visible()


@pytest.mark.e2e
def test_list_and_detail_headlines_agree_and_now_matches_board(
    page: Page, live_server_url: str, store: DashboardStore, stats_responses: dict[str, Any],
) -> None:
    open_stats(page, live_server_url + "/symphonies", store, stats_responses)
    row = page.locator('[data-stats-symphony="alpha"]')
    expect(row.locator('[data-stats-measure="completed"]')).to_have_text("1", timeout=_WAIT_LIVE)
    spend = row.locator('[data-stats-measure="spend"]').inner_text()
    row.locator('a[href="/symphonies/alpha"]').click()
    content = page.locator(CONTENT)
    expect(content).to_contain_text("Throughput", timeout=_WAIT_LIVE)
    expect(content.locator('.sym-stat').filter(has_text="Cards completed").locator("strong")).to_have_text("1")
    expect(content.locator('.sym-stat').filter(has_text="Cost").first.locator("strong")).to_have_text(spend)
    expect(content.locator('.sym-stat').filter(has_text="To Do").locator("strong")).to_have_text("1")


@pytest.mark.e2e
def test_partial_usage_and_disabled_state_are_explained(
    page: Page, live_server_url: str, store: DashboardStore, stats_responses: dict[str, Any],
) -> None:
    bodies = copy.deepcopy(stats_responses)
    for body in bodies.values():
        body["partial"] = True
        body["partial_reasons"] = ["usage_partial_line"]
        body["symphony_health"]["alpha"]["enabled"] = False
        body["symphony_health"]["alpha"]["last_poll_at"] = None
    open_stats(page, live_server_url + "/symphonies/alpha", store, bodies)
    content = page.locator(CONTENT)
    expect(content).to_contain_text("Disabled", timeout=_WAIT_LIVE)
    expect(content.locator('.sym-stat').filter(has_text="Tokens").locator("strong")).to_have_text("partial")
    expect(content.locator('.sym-stat').filter(has_text="Board check success rate").locator("strong")).to_have_text("Not recorded")

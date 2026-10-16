from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.test_dashboard_browser import _full_snapshot

WAIT_PANEL_MS = 10000
MOBILE_WIDTH = 375
MOBILE_HEIGHT = 812


def _body(window: str, agent: str) -> dict[str, Any]:
    totals = {
        "runs": 4, "success_rate": 0.75, "first_pass_rate": 0.5,
        "duration_seconds": {"median": 150, "p90": 222}, "tokens": {"total": 660},
        "cost": {"usd": 0.04}, "cost_per_run_usd": 0.01, "missing_usage_runs": 0,
        "outcomes": {"success": 3, "failure": 1}, "failure_reasons": [{"reason": "Needs tests", "runs": 1}],
    }
    return {
        "window": window, "start": "2026-10-08T00:00:00Z", "end": "2026-10-15T00:00:00Z",
        "totals": totals, "groups": [{"key": "alpha", **totals}], "definitions": {},
        "daily": [{"day": "2026-10-14", **totals}], "partial": False, "coverage": {},
        "recent_runs": [{"symphony": "alpha", "card": "card", "card_title": "Add tests", "run": "run183",
                         "started_at": "2026-10-14T12:00:00Z", "outcome": "success",
                         "duration_seconds": 60, "cost": {"usd": 0.01}}],
        "persona_versions": [{"symphony": "alpha", "version": 2, "sha256": "abc", **totals}],
        "persona_edits": [{"symphony": "alpha", "version": 2, "changed_at": "2026-10-14T00:00:00Z"}],
        "runtime": {"stage": "reviewing" if agent == "reviewer" else None,
                    "verdict_applicable": agent == "reviewer",
                    "symphonies": [{"symphony": "alpha", "persona_override": True,
                                    "model": "model-a", "mode": "fast", "hosts": ["worker"]}],
                    "activity": [{"symphony": "alpha", "card": "card", "run": "live183"}]},
    }


def _stub(page: Page) -> list[dict[str, list[str]]]:
    queries: list[dict[str, list[str]]] = []
    snapshot = _full_snapshot()
    snapshot["symphonies"] = [{"name": "alpha"}]
    page.route("**/events", lambda route: route.fulfill(
        content_type="text/event-stream", body="event: state_update\ndata: " + json.dumps(snapshot) + "\n\n",
    ))
    personas = [{"role": "reviewer", "instructions": "Review carefully."},
                {"role": "curator", "instructions": "Triage incoming issues."}]
    page.route("**/api/personas", lambda route: route.fulfill(json=personas))
    page.route("**/api/personas/*/history", lambda route: route.fulfill(json={"versions": []}))

    def stats(route: Route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        queries.append(query)
        route.fulfill(json=_body(query["window"][0], query["agent"][0]))

    page.route("**/api/stats?*", stats)
    return queries


@pytest.mark.e2e
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_persona_panel_is_read_only_accessible_and_fits_mobile(
    page: Page, live_server_url: str, theme: str,
) -> None:
    queries = _stub(page)
    page.set_viewport_size({"width": MOBILE_WIDTH, "height": MOBILE_HEIGHT})
    page.goto(live_server_url + "/personas")
    page.locator(f"[data-appearance='{theme}']").click()
    panel = page.locator("#personas-page [data-persona-panel]:visible #persona-stats-content")
    expect(panel.locator('[data-metric="tokens"]').first).to_have_text("660", timeout=WAIT_PANEL_MS)
    expect(panel.locator('[data-metric="success_rate"]').first).to_have_text("75%")
    for metric in panel.locator("[data-metric]").all():
        assert "Window: 7d" in (metric.get_attribute("title") or "")
    expect(panel).to_contain_text("Override")
    expect(panel).to_contain_text("model-a")
    expect(panel).to_contain_text("worker")
    expect(panel.locator('a[href="/runs/alpha/card/run183"]')).to_have_count(1)
    expect(panel.locator('a[href="/runs/alpha/card/live183"]')).to_have_count(1)
    expect(panel.locator('a[href="/history?symphony=alpha&h.agent=reviewer"]')).to_have_count(1)
    expect(panel.locator('svg[role="img"]')).to_have_count(3)
    assert all(chart.get_attribute("aria-label") for chart in panel.locator('svg[role="img"]').all())
    assert page.locator("#personas-page textarea").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert queries[-1]["symphony"] == ["alpha"]
    page.locator("#persona-stats-window").select_option("24h")
    expect(panel.locator('[data-metric="runs"]').first).to_have_attribute("title", re.compile("Window: 24h"))
    page.locator('#personas-page [data-persona-tab="curator"]').click()
    expect(page.locator('#personas-page [data-persona-tab="curator"]')).to_have_attribute("aria-selected", "true")
    expect(panel).to_contain_text("No pipeline stage")
    expect(page.locator('[data-persona-role="reviewer"]')).to_be_hidden()
    expect(page.locator('[data-persona-role="curator"]')).to_be_visible()
    assert queries[-1]["agent"] == ["curator"]


@pytest.mark.e2e
def test_partial_usage_and_empty_cohorts_do_not_look_precise(
    page: Page, live_server_url: str,
) -> None:
    _stub(page)
    body = _body("7d", "reviewer")
    body["totals"]["missing_usage_runs"] = 1
    page.route("**/api/stats?*", lambda route: route.fulfill(json=body))
    page.goto(live_server_url + "/personas")
    panel = page.locator("#personas-page [data-persona-panel]:visible #persona-stats-content")
    expect(panel.locator('[data-metric="tokens"]').first).to_have_text("partial", timeout=WAIT_PANEL_MS)
    expect(panel.locator('[data-metric="cost"]').first).to_have_text("partial")
    body["totals"]["runs"] = 0
    body["totals"]["missing_usage_runs"] = 0
    body["groups"] = []
    body["recent_runs"] = []
    body["persona_versions"] = []
    page.locator("#persona-stats-refresh").click()
    expect(panel).to_contain_text("No runs in this window")
    expect(panel.locator(".persona-stats-tiles")).to_have_count(0)

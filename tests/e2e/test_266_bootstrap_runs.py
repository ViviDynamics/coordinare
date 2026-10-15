"""Bootstrap page navigation, live SSE activity, retry refusals and cancellation."""
from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_266_bootstrap_visibility import _boot, _open, _symphony
from tests.e2e.test_dashboard_browser import _WAIT_LIVE

RUN_ID = "bootstrap-run-one"
JOB_ID = "bootstrap-job-one"
RUN_PATH = "/symphonies/alpha/bootstrap/" + RUN_ID
API_PATH = "**/api/symphonies/alpha/bootstrap-runs/" + RUN_ID
ACTION_PATH = "**/api/symphonies/alpha/env-bootstrap"
NEXT_ALLOWED = "2026-10-15T14:15:00+00:00"


def _detail(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": RUN_ID, "symphony": "alpha", "job_id": JOB_ID, "outcome": "running", "running": True,
        "backend": "codex", "model": "test-model", "pod": "pod-alpha", "performer_id": "perf",
        "commit": "abc1234", "started_at": "2026-10-15T13:00:00+00:00", "duration_seconds": 60,
        "last_activity_at": None, "manifest": [{"name": "python", "version": "3.12"}],
        "verify_verdict": None, "reason_tail": [], "events": [], "history": {"attempt": 1, "outcome": "running"},
    }
    result.update(overrides)
    return result


@pytest.mark.e2e
def test_bootstrap_links_open_run_with_shared_live_activity(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.route(API_PATH, lambda route: route.fulfill(json=_detail()))
    history = [{"run_id": RUN_ID, "number": 1, "attempt": 1, "outcome": "running"}]
    boot = _boot(state="bootstrapping", label="Bootstrapping", run_id=RUN_ID, history=history)
    _open(page, live_server_url + "/symphonies", store, [_symphony("alpha", boot)])
    expect(page.locator('.sym-boot-history a')).to_have_attribute("href", RUN_PATH, timeout=_WAIT_LIVE)
    link = page.locator('.sym-boot-run-link').first
    expect(link).to_be_visible(timeout=_WAIT_LIVE)
    link.click()
    expect(page.locator("#bootstrap-run-detail")).to_contain_text("test-model", timeout=_WAIT_LIVE)
    expect(page.locator("#bootstrap-run-detail")).to_contain_text("python")
    expect(page.locator("#bootstrap-transcript")).to_have_attribute("href", "/runs/" + JOB_ID)
    store.activity_log.record(activity_type="tool_use", text="Installing dependencies", session_id=RUN_ID,
                              symphony="alpha", stage="env_bootstrap")
    expect(page.locator("#bootstrap-run-events")).to_contain_text("Installing dependencies", timeout=_WAIT_LIVE)
    store.activity_log.record(activity_type="output", text="Other symphony output", session_id=RUN_ID,
                              symphony="beta", stage="env_bootstrap")
    expect(page.locator("#bootstrap-run-events")).not_to_contain_text("Other symphony output")
    assert page.url.endswith(RUN_PATH)


@pytest.mark.e2e
def test_retry_cooldown_paused_and_cancel_responses(page: Page, live_server_url: str) -> None:
    page.route(API_PATH, lambda route: route.fulfill(json=_detail()))
    page.goto(live_server_url + RUN_PATH)
    expect(page.locator("#bootstrap-retry")).to_be_visible(timeout=_WAIT_LIVE)
    page.on("dialog", lambda dialog: dialog.accept())
    page.route(ACTION_PATH, lambda route: route.fulfill(status=429, json={
        "error": "Manual bootstrap reset cooldown", "next_reset_at": NEXT_ALLOWED,
    }))
    page.locator("#bootstrap-retry").click()
    expect(page.locator("#bootstrap-action-msg")).to_contain_text(NEXT_ALLOWED)
    page.unroute(ACTION_PATH)
    page.route(ACTION_PATH, lambda route: route.fulfill(status=409, json={"error": "Symphony is paused"}))
    page.locator("#bootstrap-retry").click()
    expect(page.locator("#bootstrap-action-msg")).to_contain_text("paused")
    page.route(ACTION_PATH + "/cancel", lambda route: route.fulfill(json={"status": "cancelled", "stopped": True}))
    page.locator("#bootstrap-cancel").click()
    expect(page.locator("#bootstrap-action-msg")).to_contain_text("cancelled")

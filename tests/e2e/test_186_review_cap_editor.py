"""#186 E: the symphony override editor can set max_cards_in_review to no cap, and clear it.

The editor used to skip empty inputs, so "no cap" could only be written in YAML.
Now a "No cap (unlimited)" box saves null, an empty input removes the override, and a
number is saved as before. The symphony API is stubbed so the test sees exactly what
the page sends.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _full_snapshot

_WAIT_LIVE = 5_000
_NAME = "demo"


def _symphony(overrides: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "name": _NAME, "github_project_number": 42, "enabled": True, "overrides": overrides,
        "personas": {}, "env_spec_files": ["README.md"], "env_bootstrap_performer_id": None,
        "state": {}, "cycle_count": 0, "error_count": 0,
    }


def _snapshot(store: DashboardStore) -> None:
    store.broadcaster.broadcast(_full_snapshot(symphonies=[{"name": _NAME, "source_project": 42, "enabled": True}]))


def _stub_symphony_api(page: Page, overrides: dict[str, Any] | None, saved: list[Any]) -> None:
    """Serve the symphony and record the body of every save."""

    def handle(route: Route) -> None:
        if route.request.method == "PUT":
            saved.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", headers={"ETag": '"v2"'},
                          body=json.dumps(_symphony(saved[-1].get("overrides"))))
            return
        route.fulfill(status=200, content_type="application/json", headers={"ETag": '"v1"'},
                      body=json.dumps(_symphony(overrides)))

    page.route(f"**/api/symphonies/{_NAME}", handle)


def _open_detail(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(f"{live_server_url}/symphonies/{_NAME}")
    _snapshot(store)
    expect(page.locator("#sym-save-btn")).to_be_visible(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_no_cap_is_saved_as_an_explicit_null(page: Page, live_server_url: str, store: DashboardStore) -> None:
    saved: list[Any] = []
    _stub_symphony_api(page, {"max_concurrent_cards": 1}, saved)
    _open_detail(page, live_server_url, store)

    page.locator('[data-override-nocap="max_cards_in_review"]').check()
    expect(page.locator('[data-override-key="max_cards_in_review"]')).to_be_disabled()
    page.click("#sym-save-btn")

    expect(page.locator("#sym-save-msg")).to_have_text("Saved", timeout=_WAIT_LIVE)
    assert saved[-1]["overrides"] == {"max_concurrent_cards": 1, "max_cards_in_review": None}


@pytest.mark.e2e
def test_an_existing_no_cap_shows_checked_and_clearing_it_removes_the_override(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    saved: list[Any] = []
    _stub_symphony_api(page, {"max_cards_in_review": None}, saved)
    _open_detail(page, live_server_url, store)

    expect(page.locator('[data-override-nocap="max_cards_in_review"]')).to_be_checked()
    page.locator('[data-override-nocap="max_cards_in_review"]').uncheck()
    expect(page.locator('[data-override-key="max_cards_in_review"]')).to_be_enabled()
    page.click("#sym-save-btn")

    expect(page.locator("#sym-save-msg")).to_have_text("Saved", timeout=_WAIT_LIVE)
    assert saved[-1]["overrides"] is None


@pytest.mark.e2e
def test_a_number_still_saves_and_an_empty_input_inherits_the_global_default(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    saved: list[Any] = []
    _stub_symphony_api(page, {"max_cards_in_review": 2}, saved)
    _open_detail(page, live_server_url, store)

    page.locator('[data-override-key="max_cards_in_review"]').fill("4")
    page.click("#sym-save-btn")
    expect(page.locator("#sym-save-msg")).to_have_text("Saved", timeout=_WAIT_LIVE)
    assert saved[-1]["overrides"] == {"max_cards_in_review": 4}

    page.locator('[data-override-key="max_cards_in_review"]').fill("")
    page.click("#sym-save-btn")
    expect(page.locator("#sym-save-msg")).to_have_text("Saved", timeout=_WAIT_LIVE)
    assert saved[-1]["overrides"] is None

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_143_settings import _snapshot, _stub_config
from tests.e2e.test_dashboard_browser import _full_snapshot

_SOURCE = '#sym-config-form [data-persona-panel="implementer"] textarea'
_TEXT = "  # Local instructions\n\nKeep trailing spaces.  \n"
_NARROW = {"width": 375, "height": 800}


def _open(page: Page, url: str, store: DashboardStore, *, conflict: bool = False) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), [])
    state: dict[str, Any] = {"personas": {"qa": {"instructions": "# Local QA"}}, "etag": '"v1"'}

    def personas(route: Route) -> None:
        route.fulfill(json=[{"role": "implementer", "instructions": "# Global instructions", "is_default": False},
                            {"role": "qa", "instructions": "# Global QA", "is_default": True},
                            {"role": "advocate", "instructions": "# Advocate", "is_default": True}],
                      headers={"ETag": state["etag"]})

    def symphony(route: Route) -> None:
        if route.request.method == "PUT":
            calls.append({"method": "PUT", "body": json.loads(route.request.post_data or "{}"),
                          "etag": route.request.headers.get("if-match")})
            if conflict and state["etag"] == '"v1"':
                state["etag"] = '"v2"'
                state["personas"]["implementer"] = {"instructions": "colleague"}
                route.fulfill(status=409, json={"conflict": True})
                return
            state["personas"] = calls[-1]["body"].get("personas") or {}
            state["etag"] = '"v3"'
        route.fulfill(json={"name": "demo", "enabled": True, "github_project_number": 100,
                            "overrides": {"max_concurrent_cards": 2}, "secret_overrides": [],
                            "env_spec_files": ["README.md"], "personas": state["personas"]},
                      headers={"ETag": state["etag"]})

    def validate(route: Route) -> None:
        calls.append({"method": "validate", "body": json.loads(route.request.post_data or "{}")})
        route.fulfill(json={"valid": True})

    page.route("**/api/personas", personas)
    page.route("**/api/symphonies/demo", symphony)
    page.route("**/api/symphonies/demo/validate", validate)
    page.goto(url)
    store.broadcaster.broadcast(_full_snapshot(symphonies=[{"name": "demo", "source_project": 100, "enabled": True}]))
    return calls


def _save(page: Page) -> None:
    page.locator("#sym-save-btn").click()
    with page.expect_response(lambda response: response.request.method == "PUT"
                              and response.url.endswith("/api/symphonies/demo")) as saved:
        page.locator('#sym-config-form [data-review]').get_by_role("button", name="Save these changes").click()
    assert saved.value.status == 200
    assert saved.value.finished() is None
    expect(page.locator("#sym-save-msg")).to_have_text("Saved")


@pytest.mark.e2e
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_summary_inheritance_and_link_to_shared_config_editor(page: Page, live_server_url: str,
                                                              store: DashboardStore, theme: str) -> None:
    page.set_viewport_size(_NARROW)
    _open(page, f"{live_server_url}/symphonies/demo", store)
    page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", theme)
    summary = page.locator("#symphonies-page-section")
    expect(summary).to_contain_text("1 of 3 personas overridden: qa")
    expect(summary.locator("form, textarea, input, #sym-save-btn, #sym-delete-btn")).to_have_count(0)
    expect(summary.locator('[data-persona-panel]:visible')).to_contain_text("Global instructions")
    summary.locator('[data-persona-tab="qa"]').click()
    summary.locator('[data-persona-panel]:visible').get_by_text("Compare with global", exact=True).click()
    expect(summary.locator('[data-persona-panel]:visible pre')).to_contain_text("- # Global QA")
    expect(summary.locator('[data-persona-panel]:visible pre')).to_contain_text("+ # Local QA")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    summary.get_by_role("link", name="Edit settings", exact=True).click()
    expect(page.locator("#sym-save-btn")).to_be_visible()
    expect(page.locator('#sym-config-form [data-persona-tab]')).to_have_count(3)
    expect(page.locator('#sym-config-form [data-persona-panel]:visible')).to_contain_text("Global instructions")
    page.get_by_role("button", name="Customize for this symphony", exact=True).first.click()
    page.locator('[data-persona-panel]:visible [data-sym-start="copy"]').click()
    expect(page.locator(_SOURCE)).to_have_value("# Global instructions")
    page.locator(_SOURCE).fill(_TEXT)
    page.locator('[data-persona-panel]:visible [data-md-view="preview"]').click()
    expect(page.locator('[data-persona-panel]:visible [data-md-preview]')).to_contain_text("Local instructions")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.e2e
def test_set_exact_override_clear_and_no_edit_save(page: Page, live_server_url: str, store: DashboardStore) -> None:
    calls = _open(page, f"{live_server_url}/config#symphonies/demo/implementer", store)
    expect(page.locator("#sym-save-btn")).to_be_visible()
    page.locator("#sym-save-btn").click()
    expect(page.locator("#sym-save-msg")).to_have_text("No changes to save")
    assert calls == []
    page.locator('[data-persona-panel]:visible [data-sym-customize]').click()
    page.locator('[data-persona-panel]:visible [data-sym-start="blank"]').click()
    page.locator(_SOURCE).fill(_TEXT)
    expect(page.locator('#sym-config-form [data-sym-dirty]')).to_be_visible()
    expect(page.locator('#sym-config-form [data-sym-summary]')).to_have_text("2 of 3 personas overridden: implementer, qa")
    _save(page)
    assert [call["method"] for call in calls] == ["validate", "PUT"]
    assert calls[-1]["body"]["personas"]["implementer"]["instructions"] == _TEXT
    assert calls[-1]["etag"] == '"v1"'
    assert calls[0]["body"] == calls[1]["body"]
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator('[data-persona-panel]:visible [data-sym-revert]').click()
    expect(page.locator(_SOURCE)).to_have_value(_TEXT)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('[data-persona-panel]:visible [data-sym-revert]').click()
    expect(page.locator('[data-persona-panel]:visible [data-sym-inherited]')).to_contain_text("Global instructions")
    _save(page)
    assert "implementer" not in calls[-1]["body"]["personas"]
    expect(page.locator('#sym-config-form [data-sym-summary]')).to_have_text("1 of 3 personas overridden: qa")


@pytest.mark.e2e
def test_conflict_reapply_and_leave_protection(page: Page, live_server_url: str, store: DashboardStore) -> None:
    calls = _open(page, f"{live_server_url}/config#symphonies/demo/implementer", store, conflict=True)
    page.locator('[data-persona-panel]:visible [data-sym-customize]').click()
    page.locator('[data-persona-panel]:visible [data-sym-start="copy"]').click()
    page.locator(_SOURCE).fill(_TEXT)
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator('a.nav-link[href="/personas"]').click()
    expect(page.locator(_SOURCE)).to_have_value(_TEXT)
    page.locator("#sym-save-btn").click()
    page.locator('#sym-config-form [data-review]').get_by_role("button", name="Save these changes").click()
    page.get_by_role("button", name="Reload and reapply").click()
    expect(page.locator(_SOURCE)).to_have_value(_TEXT)
    _save(page)
    assert calls[-1]["etag"] == '"v2"'

"""#143 second review: one failing browser test per reviewed block, and the small items.

Written before the fixes. Each test pins one defect the second review reproduced.
"""
from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.test_143_settings import (
    _WAIT,
    _open_settings,
    _snapshot,
    _stub_config,
    _tab,
)


@pytest.fixture(autouse=True)
def _close_without_the_leave_prompt(page: Page) -> Generator[None, None, None]:
    """Clear the leave prompt once a test is done, so Firefox never holds a dirty page open."""
    yield
    page.evaluate(
        "() => window.addEventListener('beforeunload', (event) => event.stopImmediatePropagation(), true)",
    )


def _dirty_poll(page: Page) -> None:
    page.locator('[data-field="poll_interval_seconds"]').fill("45")


def _save_section(page: Page) -> None:
    page.locator('#config-page-section [data-section="global"] button', has_text="Save changes").click()


def _review_confirm(page: Page) -> None:
    page.locator("#config-page-section [data-review] button", has_text="Save these changes").click()


def _entry(backend: str, model: str) -> dict[str, Any]:
    return {"backend": backend, "model": model, "used_by": [],
            "target": {"base_url": f"http://{backend}.local", "wire_format": "openai", "strategy": "normalize",
                       "normalizers": []}}


# --- Block 1: a routing write names the entry it means --------------------------


@pytest.mark.e2e
def test_rt1_routing_save_after_delete_writes_the_entry_the_reader_edited(page: Page, live_server_url: str) -> None:
    """Delete A, then save B, which moved from index 1 to index 0. The save must target B, not C."""
    snap = _snapshot()
    snap["routing_available"] = True
    snap["sections"][6]["invalid_banner"] = None
    state: dict[str, Any] = {"entries": [_entry("A", "a"), _entry("B", "b"), _entry("C", "c")], "hash": "h1"}
    calls: list[dict[str, Any]] = []
    _stub_config(page, snap, [])

    def routing_get(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"content_hash": state["hash"], "entries": state["entries"]}))

    def entry_write(route: Route) -> None:
        idx = int(route.request.url.rsplit("/", 1)[-1])
        body = json.loads(route.request.post_data or "{}")
        calls.append({"method": route.request.method, "index": idx, "body": body})
        if route.request.method == "DELETE":
            del state["entries"][idx]
        else:
            state["entries"][idx]["model"] = body.get("changes", {}).get("model", state["entries"][idx]["model"])
        state["hash"] = state["hash"] + "x"
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "new_hash": state["hash"], "applied": "staged_next_job"}))

    page.route("**/api/config/routing", routing_get)
    page.route("**/api/config/routing/entry/*", entry_write)
    _open_settings(page, live_server_url)
    _tab(page, "Routing")
    expect(page.locator("#cfg-routing-entries [data-rentry]")).to_have_count(3, timeout=_WAIT)
    blocks = page.locator("#cfg-routing-entries [data-rentry]")
    blocks.nth(2).locator('[data-rkey="model"]').fill("c-typed")
    blocks.nth(0).locator('button[data-cfg-action="delete-routing"]').click()
    page.locator("#cfg-routing-entries [data-review] button", has_text="Save these changes").click()
    expect(page.locator("#cfg-routing-entries [data-rentry]")).to_have_count(2, timeout=_WAIT)
    blocks = page.locator("#cfg-routing-entries [data-rentry]")
    blocks.nth(0).locator('[data-rkey="model"]').fill("b-edit")
    blocks.nth(0).locator('button[data-cfg-action="save-routing"]').click()
    page.locator("#cfg-routing-entries [data-review] button", has_text="Save these changes").click()
    # The list is drawn again after a write, so the proof is the redrawn entry, not the status line.
    expect(page.locator("#cfg-routing-entries [data-rentry]", has_text="B / b-edit")).to_be_visible(timeout=_WAIT)
    puts = [c for c in calls if c["method"] == "PUT"]
    assert puts and puts[-1]["index"] == 0, f"the save on B went to index {puts[-1]['index'] if puts else None}"
    assert puts[-1]["body"].get("expected") == {"backend": "B", "model": "b"}
    # The unsaved edit on C survives the list being redrawn after the delete.
    expect(page.locator("#cfg-routing-entries [data-rentry]", has_text="C / c")
           .locator('[data-rkey="model"]')).to_have_value("c-typed", timeout=_WAIT)


# --- Block 2: a secret can be cleared, and the review says so -------------------


@pytest.mark.e2e
def test_s1_a_secret_can_be_cleared_from_the_ui(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url)
    # 225: github_token is required under pat auth, so only an optional secret has a Clear.
    page.locator('#config-page-section [data-clear-for="dashboard_auth_token"]').click()
    _dirty_poll(page)
    _save_section(page)
    expect(page.locator("#config-page-section [data-review]")).to_contain_text("(cleared)", timeout=_WAIT)
    _review_confirm(page)
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text("Saved", timeout=_WAIT)
    assert saves and saves[0]["body"]["changes"].get("dashboard_auth_token") == ""


@pytest.mark.e2e
def test_s2_the_typed_mask_is_never_sent_as_a_secret(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url)
    page.locator('#config-page-section [data-field="github_token"]').fill("••••••")
    _dirty_poll(page)
    _save_section(page)
    _review_confirm(page)
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text("Saved", timeout=_WAIT)
    assert saves and "github_token" not in saves[0]["body"]["changes"]


@pytest.mark.e2e
def test_s3_a_secret_input_is_a_password_field(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url)
    token = page.locator('#config-page-section [data-field="github_token"]')
    expect(token).to_have_attribute("type", "password")
    expect(token).to_have_attribute("autocomplete", "off")


# --- Block 3: the auto-reload does not wipe typing ------------------------------


@pytest.mark.e2e
def test_r1_typing_within_a_second_of_a_save_survives_the_reload(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url)
    _tab(page, "Model servers")
    _tab(page, "General")
    _dirty_poll(page)
    _save_section(page)
    _review_confirm(page)
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text("Saved", timeout=_WAIT)
    _tab(page, "Model servers")
    page.evaluate("cfgToggleAdd('endpoints')")
    page.locator("#cfg-add-endpoints [data-field='kind']").select_option("openai")
    page.wait_for_timeout(2000)
    expect(page.locator("#cfg-add-endpoints [data-field='kind']")).to_have_value("openai")


# --- Rejected saves do not replay silently ---------------------------------------


@pytest.mark.e2e
def test_r2_a_rejected_save_is_not_replayed_when_the_page_is_revisited(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])

    def invalid(route: Route) -> None:
        route.fulfill(status=422, content_type="application/json", body=json.dumps({
            "ok": False, "errors": [{"key": "poll_interval_seconds", "code": "validation", "message": "too large"}]}))

    page.route("**/api/config/section/*", invalid)
    _open_settings(page, live_server_url)
    _dirty_poll(page)
    _save_section(page)
    _review_confirm(page)
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).not_to_contain_text(
        "Saved", timeout=_WAIT,
    )
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#navbar a.nav-link", has_text="Dashboard").click()
    expect(page.locator("#dashboard-page")).to_be_visible(timeout=_WAIT)
    page.locator("#navbar a.nav-link", has_text="Settings").click()
    expect(page.locator("#config-page-section [role=tab]").first).to_be_visible(timeout=_WAIT)
    expect(page.locator('[data-field="poll_interval_seconds"]')).to_have_value("30", timeout=_WAIT)


# --- A hung save does not keep the form locked -----------------------------------


@pytest.mark.e2e
def test_timeout_a_hung_save_unlocks_the_form_with_a_message(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    page.route("**/api/config/section/*", lambda route: None)
    _open_settings(page, live_server_url)
    page.evaluate("() => { CFG_SAVE_TIMEOUT_MS = 1500; }")
    _dirty_poll(page)
    _save_section(page)
    _review_confirm(page)
    status = page.locator('#config-page-section [data-section="global"] [data-status]')
    expect(status).to_contain_text("did not finish", timeout=_WAIT * 2)
    expect(page.locator('[data-field="poll_interval_seconds"]')).to_be_enabled()


# --- Persona reset says what it discards ------------------------------------------


@pytest.mark.e2e
def test_persona_reset_says_unsaved_text_will_be_discarded(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url, "/config#personas")
    implementer = page.locator('[data-scope="item:personas/implementer"]')
    implementer.locator("textarea").fill("Unsaved text I typed.")
    implementer.locator("button", has_text="Reset to default").click()
    review = implementer.locator("[data-review]")
    expect(review).to_be_visible(timeout=_WAIT)
    expect(review).to_contain_text("discarded")


# --- Symphony suffix keeps the Settings hash -------------------------------------


@pytest.mark.e2e
def test_sy1_the_symphony_query_goes_before_the_hash(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    page.goto(live_server_url)
    expect(page.locator("#dashboard-page")).to_be_visible(timeout=_WAIT)
    out = page.evaluate("""() => {
        _lastState = {symphonies: [{name: 'alpha'}, {name: 'beta'}]};
        writeStoredSymphony('beta');
        return pathWithSymphony('/config#personas/implementer');
    }""")
    assert "#" in out and out.index("?") < out.index("#"), out
    assert out.endswith("personas/implementer"), out

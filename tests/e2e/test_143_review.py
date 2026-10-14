"""#143 review round: one browser test per reviewed block, plus the XSS and history items.

Each test pins one reviewed defect. They fail on the review's build and pass once the
block is fixed. The stubs and helpers are the ones test_143_settings.py uses.
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
    _setting,
    _snapshot,
    _stub_config,
    _tab,
)


@pytest.fixture(autouse=True)
def _close_without_the_leave_prompt(page: Page) -> Generator[None, None, None]:
    """Leave each test's page with its leave prompt cleared.

    Several tests end with unsaved edits on the page. Firefox then holds the page open for
    the beforeunload prompt, and the next test stalls. The capture listener stops the prompt
    once the test's own assertions are done.
    """
    yield
    page.evaluate(
        "() => window.addEventListener('beforeunload', (event) => event.stopImmediatePropagation(), true)",
    )


def _routing_entry(backend: str, model: str, base_url: str) -> dict[str, Any]:
    return {
        "backend": backend, "model": model, "used_by": [],
        "target": {"base_url": base_url, "wire_format": "openai", "strategy": "normalize", "normalizers": []},
    }


def _routing_snapshot() -> dict[str, Any]:
    snap = _snapshot()
    snap["routing_available"] = True
    routing = next(sec for sec in snap["sections"] if sec["id"] == "routing")
    routing["invalid_banner"] = None
    return snap


def _endpoint_create_snapshot() -> dict[str, Any]:
    """The Model servers tab, whose create form has a name field to type into."""
    snap = _snapshot()
    endpoints = next(sec for sec in snap["sections"] if sec["id"] == "endpoints")
    endpoints["items"][0]["settings"].insert(0, _setting("endpoints.name", "Name", "string", "local-vllm"))
    return snap


# --- Block 1: the sent value is the reviewed value; the form is locked while reviewed ---


@pytest.mark.e2e
def test_block1_the_sent_value_is_the_reviewed_value_and_the_form_is_locked(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url)
    field = page.locator('[data-field="poll_interval_seconds"]')
    field.fill("45")
    page.locator('#config-page-section [data-section="global"] button', has_text="Save changes").click()
    box = page.locator("#config-page-section [data-review]")
    expect(box).to_contain_text("45")
    expect(field).to_be_disabled()
    box.locator("button", has_text="Save these changes").click()
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text("Saved", timeout=_WAIT)
    assert saves[0]["body"]["changes"] == {"poll_interval_seconds": 45}


# --- Block 2: a typed new entry is unsaved work ---------------------------------


@pytest.mark.e2e
def test_block2_a_typed_new_entry_is_unsaved_work_when_leaving(page: Page, live_server_url: str) -> None:
    _stub_config(page, _endpoint_create_snapshot(), [])
    _open_settings(page, live_server_url)
    asked: list[str] = []
    page.on("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
    _tab(page, "Model servers")
    page.locator("#config-page-section button", has_text="+ Add entry").first.click()
    page.locator('#cfg-add-endpoints [data-field="name"]').fill("newone")
    page.locator("#navbar a.nav-link", has_text="Dashboard").click()
    page.wait_for_timeout(300)
    assert asked and "unsaved changes" in asked[0]
    assert page.url.startswith(f"{live_server_url}/config"), page.url


@pytest.mark.e2e
def test_block2_a_typed_new_routing_entry_is_unsaved_work_when_leaving(page: Page, live_server_url: str) -> None:
    _stub_config(page, _routing_snapshot(), [])
    page.route("**/api/config/routing", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"entries": [], "content_hash": "r1"}),
    ))
    _open_settings(page, live_server_url)
    asked: list[str] = []
    page.on("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
    _tab(page, "Routing")
    page.locator("#config-page-section button", has_text="+ Add routing entry").click()
    page.locator('#cfg-add-routing [data-rkey="backend"]').fill("vllm")
    page.locator("#navbar a.nav-link", has_text="Dashboard").click()
    page.wait_for_timeout(300)
    assert asked and "unsaved changes" in asked[0]


# --- Block 3: reload and reapply puts a refused new entry back ----------------


@pytest.mark.e2e
def test_block3_reload_and_reapply_puts_a_refused_new_entry_back(page: Page, live_server_url: str) -> None:
    _stub_config(page, _endpoint_create_snapshot(), [])
    page.route("**/api/config/catalog/**", lambda route: route.fulfill(
        status=409, content_type="application/json", body=json.dumps({"ok": False, "conflict": True, "error": "x"}),
    ))
    _open_settings(page, live_server_url)
    _tab(page, "Model servers")
    page.locator("#config-page-section button", has_text="+ Add entry").first.click()
    page.locator('#cfg-add-endpoints [data-field="name"]').fill("newone")
    page.locator('#cfg-add-endpoints [data-field="kind"]').select_option("openai")
    page.locator("#cfg-add-endpoints button", has_text="Create").click()
    page.locator("#cfg-add-endpoints [data-review] button", has_text="Save these changes").click()
    expect(page.locator("#config-page-section")).to_contain_text("Your edits are still here", timeout=_WAIT)
    page.locator("#cfg-add-endpoints button", has_text="Reload and reapply").click()
    expect(page.locator('#cfg-add-endpoints [data-field="name"]')).to_have_value("newone", timeout=_WAIT)
    expect(page.locator('#cfg-add-endpoints [data-field="kind"]')).to_have_value("openai")
    expect(page.locator("#cfg-add-endpoints")).to_be_visible()


# --- Block 4: a saved secret is not left in the DOM ---------------------------


@pytest.mark.e2e
def test_block4_a_saved_secret_is_not_left_in_the_dom(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url)
    token = page.locator('#config-page-section [data-field="github_token"]')
    token.fill("ghp_NEWSECRETVALUE")
    page.locator('#config-page-section [data-section="global"] button', has_text="Save changes").click()
    page.locator("#config-page-section [data-review] button", has_text="Save these changes").click()
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text("Saved", timeout=_WAIT)
    assert "NEWSECRETVALUE" not in (token.get_attribute("data-orig") or "")
    assert token.input_value() == ""
    assert "NEWSECRETVALUE" not in page.content()


# --- XSS: an id with quotes is data, never code --------------------------------

_HOSTILE_ID = 'a"+(window.__pwn=1)+"'


@pytest.mark.e2e
def test_xss_a_hostile_item_id_cannot_run_script_on_save(page: Page, live_server_url: str) -> None:
    snap = _snapshot()
    snap["sections"][3]["items"][0]["id"] = _HOSTILE_ID
    _stub_config(page, snap, [])
    _open_settings(page, live_server_url)
    _tab(page, "Model servers")
    page.locator("#cfg-panel-endpoints [data-item] select").first.select_option("openai")
    page.locator("#cfg-panel-endpoints [data-item] button", has_text="Save").first.click()
    page.locator("#cfg-panel-endpoints [data-item] [data-review] button", has_text="Save these changes").click()
    page.wait_for_timeout(300)
    assert page.evaluate("() => window.__pwn === undefined"), "the hostile id ran as script"
    # The id is shown as data, so the text can appear in an attribute value. No inline handler may carry it.
    assert page.locator('[onclick*="__pwn"]').count() == 0


# --- Routing: a reapplied edit follows the entry, not its position -------------


@pytest.mark.e2e
def test_routing_reapply_lands_on_the_same_entry_after_the_list_moves(page: Page, live_server_url: str) -> None:
    state: dict[str, Any] = {
        "entries": [_routing_entry("vllm", "m1", "http://a"), _routing_entry("vllm", "m2", "http://b")],
        "conflict": False,
    }

    def routing(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"entries": state["entries"], "content_hash": "r1"}))

    def save(route: Route) -> None:
        if state["conflict"]:
            route.fulfill(status=409, content_type="application/json",
                          body=json.dumps({"ok": False, "conflict": True, "error": "x"}))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "new_hash": "r2"}))

    _stub_config(page, _routing_snapshot(), [])
    page.route("**/api/config/routing", routing)
    page.route("**/api/config/routing/entry/*", save)
    _open_settings(page, live_server_url)
    _tab(page, "Routing")
    entry = page.locator("[data-rentry]", has_text="vllm / m2")
    expect(entry).to_be_visible(timeout=_WAIT)
    entry.locator('[data-rkey="target.base_url"]').fill("http://edited")
    # A new entry lands in front while the save is in flight, so m2 moves from index 1 to index 2.
    state["entries"] = [_routing_entry("vllm", "m0", "http://c")] + state["entries"]
    state["conflict"] = True
    entry.locator("button", has_text="Save").click()
    entry.locator("[data-review] button", has_text="Save these changes").click()
    expect(entry.locator("[data-status]")).to_contain_text("Your edits are still here", timeout=_WAIT)
    entry.locator("[data-status] button", has_text="Reload and reapply").click()
    expect(page.locator("[data-rentry]", has_text="vllm / m2").locator('[data-rkey="target.base_url"]')).to_have_value(
        "http://edited", timeout=_WAIT,
    )
    expect(page.locator("[data-rentry]", has_text="vllm / m1").locator('[data-rkey="target.base_url"]')).to_have_value(
        "http://a", timeout=_WAIT,
    )


# --- Persona reset goes through the review ------------------------------------


@pytest.mark.e2e
def test_persona_reset_is_reviewed_in_the_page_not_a_browser_prompt(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url, "/config#personas")
    prompts: list[str] = []
    page.on("dialog", lambda dialog: (prompts.append(dialog.type), dialog.dismiss()))
    implementer = page.locator('[data-scope="item:personas/implementer"]')
    implementer.locator("button", has_text="Reset to default").click()
    expect(implementer.locator("[data-review]")).to_be_visible(timeout=_WAIT)
    assert prompts == []
    implementer.locator("[data-review] button", has_text="Save these changes").click()
    # A reset reloads the page, so the review is gone and the request is what proves it was sent.
    expect(implementer.locator("[data-review]")).to_have_count(0, timeout=_WAIT)
    assert saves and saves[0]["method"] == "DELETE"


# --- Declining Back: the exact URL comes back, with no extra history entry ----


@pytest.mark.e2e
def test_declining_back_restores_the_exact_url_and_adds_no_history_entry(page: Page, live_server_url: str) -> None:
    # Arrive from the Personas page, so the entry Back returns to is another page, and the
    # Settings entry carries a hash (#personas/implementer) that must come back exactly.
    _stub_config(page, _snapshot(), [])
    page.goto(f"{live_server_url}/personas")
    page.locator("#personas-page-section a", has_text="Edit in Settings").first.click()
    expect(page.locator("#config-page")).to_be_visible(timeout=_WAIT)
    expect(page).to_have_url(f"{live_server_url}/config#personas/implementer", timeout=_WAIT)
    url_before = page.url
    length_before = page.evaluate("() => history.length")
    persona = page.locator('[data-scope="item:personas/implementer"] textarea')
    persona.fill("Edited, not yet saved.")
    page.on("dialog", lambda dialog: dialog.dismiss())
    # Back is started from a timer, so the page is not blocked while the leave prompt is answered.
    page.evaluate("() => setTimeout(() => history.back(), 0)")
    page.wait_for_timeout(1000)
    expect(page).to_have_url(url_before, timeout=_WAIT)
    expect(page.locator("#config-page")).to_be_visible(timeout=_WAIT)
    assert page.evaluate("() => history.length") == length_before
    expect(persona).to_have_value("Edited, not yet saved.")


@pytest.mark.e2e
def test_an_untouched_unset_field_does_not_prompt_when_leaving(page: Page, live_server_url: str) -> None:
    """An optional field with no value is not an edit: leaving an untouched page never asks."""
    snap = _snapshot()
    endpoints = next(sec for sec in snap["sections"] if sec["id"] == "endpoints")
    endpoints["items"][0]["settings"].insert(0, _setting("endpoints.base_url", "Base URL", "string", None))
    _stub_config(page, snap, [])
    _open_settings(page, live_server_url)
    asked: list[str] = []
    page.on("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
    page.locator("#navbar a.nav-link", has_text="Dashboard").click()
    expect(page.locator("#dashboard-page")).to_be_visible(timeout=_WAIT)
    assert asked == []

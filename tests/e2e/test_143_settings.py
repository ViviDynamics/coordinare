"""#143: the Settings page in a real browser, with the config API stubbed.

Each setting is edited in one place, each tab says what it is, the old addresses
redirect, leaving with unsaved edits asks first, a save shows what changes (never a
secret), a concurrent edit is explained with the edits kept, and each item says what
uses it. Runs in Chromium and Firefox (--browser firefox).
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Dialog, Locator, Page, Route, expect

_WAIT = 5000

_TITLES = {
    "global": "General",
    "personas": "Agents",
    "symphonies": "Symphonies",
    "endpoints": "Model servers",
    "model_endpoints": "Models",
    "modes": "Modes",
    "routing": "Routing",
}


def _setting(key: str, label: str, type_: str, current: Any, *, editable: bool = True, **extra: Any) -> dict[str, Any]:
    base = {
        "key": key, "label": label, "help": None, "type": type_, "current_value": current,
        "default": None, "range": None, "enum": None, "editable": editable, "restart_required": False,
        "secret": False, "is_env_placeholder": False, "invalid": False,
        "section": key.split(".")[0], "store": "config_yaml",
    }
    base.update(extra)
    return base


def _snapshot(poll: int = 30) -> dict[str, Any]:
    sections = [
        {
            "id": "global", "title": "General", "store": "config_yaml", "kind": "scalar_group",
            "description": "Settings shared by every symphony, unless a symphony overrides them.",
            "items": [], "invalid_banner": None,
            "settings": [
                _setting("global.poll_interval_seconds", "Poll interval (seconds)", "int", poll),
                _setting("global.github_token", "GitHub token", "secret", "••••••", secret=True),
            ],
        },
        {
            "id": "personas", "title": "Agents", "store": "config_yaml", "kind": "collection",
            "description": "The instructions each agent role is given.",
            "settings": [], "invalid_banner": None,
            "items": [
                {"id": "implementer", "kind": "persona", "settings": [], "referenced_by": [], "deletable": True,
                 "used_by": [{"kind": "stage", "name": "implementing"}, {"kind": "symphony", "name": "demo"}]},
                {"id": "reviewer", "kind": "persona", "settings": [], "referenced_by": [], "deletable": True,
                 "used_by": []},
            ],
        },
        {
            "id": "symphonies", "title": "Symphonies", "store": "config_yaml", "kind": "collection",
            "description": "Each symphony's settings. Edit them on the symphony's page, which is linked from each entry here.",
            "settings": [], "invalid_banner": None,
            "items": [{
                "id": "demo", "kind": "symphony", "referenced_by": [], "deletable": True,
                "settings": [_setting("symphonies.github_project_number", "GitHub project number", "int", 100)],
            }],
        },
        {
            "id": "endpoints", "title": "Model servers", "store": "config_yaml", "kind": "collection",
            "description": "Where model calls go: a self-hosted server at a base URL, or a vendor API.",
            "settings": [], "invalid_banner": None,
            "items": [{
                "id": "local-vllm", "kind": "endpoints", "referenced_by": ["qwen-coder"], "deletable": False,
                "used_by": [{"kind": "model", "name": "qwen-coder"}, {"kind": "mode", "name": "single-qwen"},
                            {"kind": "stage", "name": "implementing"}, {"kind": "symphony", "name": "demo"}],
                "settings": [_setting("endpoints.kind", "Kind", "enum", "vllm", enum=["vllm", "openai"])],
            }],
        },
        {
            "id": "model_endpoints", "title": "Models", "store": "config_yaml", "kind": "collection",
            "description": "A model name served by one model server. Modes choose their models from this list.",
            "settings": [], "invalid_banner": None,
            "items": [{
                "id": "qwen-coder", "kind": "model_endpoints", "referenced_by": ["single-qwen"], "deletable": False,
                "used_by": [{"kind": "mode", "name": "single-qwen"}, {"kind": "stage", "name": "implementing"},
                            {"kind": "symphony", "name": "demo"}],
                "settings": [_setting("model_endpoints.model", "Model", "string", "qwen2.5-coder")],
            }],
        },
        {
            "id": "modes", "title": "Modes", "store": "config_yaml", "kind": "collection",
            "description": "How a role uses models: one model, or a planner and an executor.",
            "settings": [], "invalid_banner": None,
            "items": [{
                "id": "single-qwen", "kind": "modes", "referenced_by": ["implementer"], "deletable": False,
                "used_by": [{"kind": "stage", "name": "implementing"}, {"kind": "symphony", "name": "demo"}],
                "settings": [_setting("modes.strategy", "Strategy", "enum", "single", enum=["single"])],
            }],
        },
        {
            "id": "routing", "title": "Routing", "store": "routing_yaml", "kind": "collection",
            "description": (
                "Which base URL and wire format a self-hosted backend is sent to, "
                "for a given backend and model. Applies to the next performer job, not live."
            ),
            "settings": [], "items": [],
            "invalid_banner": "No performer routing table is mounted on this host, so routing is not editable here.",
        },
    ]
    return {
        "sections": sections,
        "content_hashes": {"config_yaml": "hash-1"},
        "config_version": 0,
        "routing_available": False,
        "routing_mounted": False,
        "symphony_overrides": {},
    }


def _stub_config(page: Page, snapshot: dict[str, Any], saves: list[dict[str, Any]], *, save_status: int = 200,
                 after_conflict: dict[str, Any] | None = None) -> None:
    """Serve the config and personas API. Every save is recorded in `saves`."""
    state = {"snapshot": snapshot}

    def all_config(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", headers={"ETag": '"hash-1"'},
                      body=json.dumps(state["snapshot"]))

    def personas(route: Route) -> None:
        body = [
            {"role": "implementer", "instructions": "Implement the change.", "is_default": True},
            {"role": "reviewer", "instructions": "Review carefully.", "is_default": False},
        ]
        route.fulfill(status=200, content_type="application/json", headers={"ETag": '"persona-1"'},
                      body=json.dumps(body))

    def section_save(route: Route) -> None:
        saves.append({"url": route.request.url, "body": json.loads(route.request.post_data or "{}"),
                      "method": route.request.method})
        if save_status == 409 and after_conflict is not None:
            state["snapshot"] = after_conflict
        if save_status == 200:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "applied": "hot_reloaded", "new_hash": "hash-2"}))
        else:
            route.fulfill(status=save_status, content_type="application/json",
                          body=json.dumps({"ok": False, "conflict": save_status == 409,
                                           "error": "Config changed since this page loaded."}))

    def persona_save(route: Route) -> None:
        saves.append({"url": route.request.url, "body": json.loads(route.request.post_data or "{}"),
                      "method": route.request.method, "if_match": route.request.headers.get("if-match")})
        route.fulfill(status=204, headers={"ETag": '"persona-2"'})

    page.route("**/api/config/all", all_config)
    page.route("**/api/personas", personas)
    page.route("**/api/config/section/*", section_save)
    page.route("**/api/personas/*", persona_save)


def _open_settings(page: Page, live_server_url: str, path: str = "/config") -> None:
    page.goto(f"{live_server_url}{path}")
    expect(page.locator("#config-page")).to_be_visible(timeout=_WAIT)
    expect(page.locator("#config-page-section [role=tab]").first).to_be_visible(timeout=_WAIT)


def _tab(page: Page, title: str) -> None:
    page.locator("#config-page-section [role=tab]", has_text=title).first.click()


def _active_panel(page: Page) -> Locator:
    return page.locator("#config-page-section [role=tabpanel]:not([hidden])")


# --- one place per setting, one description per tab -----------------------------


@pytest.mark.e2e
def test_settings_is_the_one_configuration_item_in_the_nav(page: Page, live_server_url: str) -> None:
    page.goto(live_server_url)
    expect(page.locator("#navbar a.nav-link", has_text="Settings")).to_have_count(1)
    expect(page.locator("#navbar a.nav-link", has_text="Global Config")).to_have_count(0)
    expect(page.locator("#navbar a[href='/config']")).to_have_count(1)


@pytest.mark.e2e
def test_each_setting_is_edited_in_one_place(page: Page, live_server_url: str) -> None:
    """The General poll interval is an input on Settings and nowhere else in the dashboard."""
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url)
    expect(page.locator('#config-page-section [data-field="poll_interval_seconds"]')).to_have_count(1)
    # The Personas page is read-only: it has no persona text box and no save button.
    page.route("**/api/personas", lambda route: route.fulfill(
        status=200, content_type="application/json", headers={"ETag": '"persona-1"'},
        body=json.dumps([{"role": "implementer", "instructions": "Implement the change.", "is_default": True}]),
    ))
    page.goto(f"{live_server_url}/personas")
    expect(page.locator("#personas-page-section")).to_contain_text("Implement the change.", timeout=_WAIT)
    expect(page.locator("#personas-page textarea")).to_have_count(0)
    expect(page.locator("#personas-page button", has_text="Save")).to_have_count(0)
    expect(page.locator("#personas-page a", has_text="Edit in Settings")).to_have_attribute(
        "href", "/config#personas/implementer",
    )


@pytest.mark.e2e
def test_each_tab_has_a_name_and_a_one_line_description(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url)
    tabs = page.locator("#config-page-section [role=tab]")
    expect(tabs).to_have_count(7)
    for key, title in _TITLES.items():
        _tab(page, title)
        panel = _active_panel(page)
        expect(panel.locator("h3").first).to_have_text(title)
        description = panel.locator("h3 + div").first
        expect(description).not_to_be_empty()
        assert "\n" not in (description.inner_text() or "")
        assert key in _TITLES


@pytest.mark.e2e
def test_the_tab_names_say_what_each_one_is(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url)
    _tab(page, "Model servers")
    expect(_active_panel(page)).to_contain_text("Where model calls go")
    _tab(page, "Modes")
    expect(_active_panel(page)).to_contain_text("planner and an executor")
    _tab(page, "Routing")
    expect(_active_panel(page)).to_contain_text("Applies to the next performer job")


# --- old addresses ---------------------------------------------------------------


@pytest.mark.e2e
def test_the_global_config_address_redirects_to_the_general_tab(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    page.goto(f"{live_server_url}/admin/config?from=bookmark")
    expect(page).to_have_url(f"{live_server_url}/config?from=bookmark#global", timeout=_WAIT)
    expect(page.locator("#config-page")).to_be_visible(timeout=_WAIT)
    expect(page.locator("#config-page-section [role=tab][aria-selected=true]")).to_have_text("General")


@pytest.mark.e2e
def test_an_in_app_link_to_the_old_address_lands_on_general(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url)
    page.evaluate("() => { history.pushState(null, '', '/admin/config'); router(); }")
    expect(page).to_have_url(f"{live_server_url}/config#global", timeout=_WAIT)
    expect(page.locator("#config-page")).to_be_visible(timeout=_WAIT)


@pytest.mark.e2e
def test_a_deep_link_opens_the_right_tab_and_item(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url, "/config#personas/implementer")
    expect(page.locator("#config-page-section [role=tab][aria-selected=true]")).to_have_text("Agents", timeout=_WAIT)
    expect(page.locator('[data-scope="item:personas/implementer"] textarea')).to_be_focused(timeout=_WAIT)


# --- leaving with unsaved edits -------------------------------------------------


@pytest.mark.e2e
def test_leaving_with_unsaved_edits_asks_first_and_can_be_declined(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url)
    asked: list[str] = []

    def decline(dialog: Dialog) -> None:
        asked.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", decline)
    page.locator('[data-field="poll_interval_seconds"]').fill("45")
    page.locator("#navbar a.nav-link", has_text="Dashboard").click()
    expect(page.locator("#config-page")).to_be_visible(timeout=_WAIT)
    assert asked and "unsaved changes" in asked[0]
    expect(page).to_have_url(f"{live_server_url}/config", timeout=_WAIT)


_CANCELLABLE_UNLOAD = """() => {
    const event = new Event('beforeunload', {cancelable: true});
    window.dispatchEvent(event);
    return event.defaultPrevented;
}"""


@pytest.mark.e2e
def test_closing_the_page_with_unsaved_edits_asks_the_browser(page: Page, live_server_url: str) -> None:
    """The page cancels an unload while it holds unsaved edits.

    Checked by dispatching the event: Firefox does not hand a beforeunload prompt to
    Playwright's dialog hook, so the prompt itself cannot be observed there.
    """
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url)
    assert page.evaluate(_CANCELLABLE_UNLOAD) is False
    page.locator('[data-field="poll_interval_seconds"]').fill("45")
    assert page.evaluate(_CANCELLABLE_UNLOAD) is True


@pytest.mark.e2e
def test_an_untouched_form_does_not_ask(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url)
    asked: list[str] = []
    page.on("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
    page.locator("#navbar a.nav-link", has_text="Dashboard").click()
    expect(page.locator("#dashboard-page")).to_be_visible(timeout=_WAIT)
    assert asked == []


@pytest.mark.e2e
def test_a_saved_edit_does_not_ask_when_leaving(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url)
    asked: list[str] = []
    page.on("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
    page.locator('[data-field="poll_interval_seconds"]').fill("45")
    page.locator("#config-page-section button", has_text="Save changes").click()
    page.locator("#config-page-section button", has_text="Save these changes").click()
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text("Saved", timeout=_WAIT)
    page.locator("#navbar a.nav-link", has_text="Dashboard").click()
    expect(page.locator("#dashboard-page")).to_be_visible(timeout=_WAIT)
    assert asked == []


# --- the review before a save ----------------------------------------------------


@pytest.mark.e2e
def test_a_save_shows_old_and_new_and_never_a_secret(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url)
    page.locator('[data-field="poll_interval_seconds"]').fill("45")
    page.locator('[data-field="github_token"]').fill("ghp_TYPEDSECRET123")
    page.locator('#config-page-section [data-section="global"] button', has_text="Save changes").click()

    review = page.locator('#config-page-section [data-review]')
    expect(review).to_be_visible()
    expect(review).to_contain_text("Review your changes before saving")
    expect(review).to_contain_text("Poll interval (seconds)")
    expect(review).to_contain_text("Was: 30")
    expect(review).to_contain_text("Now: 45")
    expect(review).to_contain_text("GitHub token")
    body_text = page.locator("#config-page-section").inner_text()
    assert "ghp_TYPEDSECRET123" not in body_text
    assert "••••••" not in review.inner_text()
    assert saves == [], "nothing is sent before the review is confirmed"


@pytest.mark.e2e
def test_keep_editing_sends_nothing_and_confirm_sends_once(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url)
    page.locator('[data-field="poll_interval_seconds"]').fill("45")
    page.locator('#config-page-section [data-section="global"] button', has_text="Save changes").click()
    page.locator('#config-page-section [data-review] button', has_text="Keep editing").click()
    expect(page.locator("#config-page-section [data-review]")).to_have_count(0)
    expect(page.locator('[data-field="poll_interval_seconds"]')).to_have_value("45")
    assert saves == []
    page.locator('#config-page-section [data-section="global"] button', has_text="Save changes").click()
    page.locator('#config-page-section [data-review] button', has_text="Save these changes").click()
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text("Saved", timeout=_WAIT)
    assert len(saves) == 1
    assert saves[0]["body"]["changes"] == {"poll_interval_seconds": 45}
    assert saves[0]["body"]["base_hash"] == "hash-1"


# --- a concurrent edit ------------------------------------------------------------


@pytest.mark.e2e
def test_a_concurrent_edit_is_explained_and_reapplied_after_reload(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    # Someone else changed the poll interval to 33 while this page was open.
    _stub_config(page, _snapshot(), saves, save_status=409, after_conflict=_snapshot(poll=33))
    _open_settings(page, live_server_url)
    page.locator('[data-field="poll_interval_seconds"]').fill("45")
    page.locator('#config-page-section [data-section="global"] button', has_text="Save changes").click()
    page.locator('#config-page-section [data-review] button', has_text="Save these changes").click()

    status = page.locator('#config-page-section [data-section="global"] [data-status]')
    expect(status).to_contain_text("Someone else changed these settings after this page loaded", timeout=_WAIT)
    expect(status).to_contain_text("Your edits are still here")
    expect(page.locator('[data-field="poll_interval_seconds"]')).to_have_value("45")

    page.locator('#config-page-section [data-section="global"] [data-status] button', has_text="Reload and reapply").click()
    expect(page.locator('[data-field="poll_interval_seconds"]')).to_have_value("45", timeout=_WAIT)
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text(
        "Your edits are back in the form", timeout=_WAIT,
    )


# --- Agents: the one persona editor ---------------------------------------------


@pytest.mark.e2e
def test_an_agent_edit_goes_through_the_review_with_its_version(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    _open_settings(page, live_server_url, "/config#personas")
    implementer = page.locator('[data-scope="item:personas/implementer"]')
    expect(implementer).to_be_visible(timeout=_WAIT)
    implementer.locator("textarea").fill("Implement the change, then test it.")
    implementer.locator("button", has_text="Save").click()
    expect(implementer.locator("[data-review]")).to_contain_text("Implement the change, then test it.")
    implementer.locator("[data-review] button", has_text="Save these changes").click()
    expect(implementer.locator("[data-status]")).to_contain_text("Saved", timeout=_WAIT)
    assert len(saves) == 1
    assert saves[0]["url"].endswith("/api/personas/implementer")
    assert saves[0]["method"] == "PUT"
    assert saves[0]["body"] == {"instructions": "Implement the change, then test it."}
    assert saves[0]["if_match"] == '"persona-1"'


@pytest.mark.e2e
def test_used_by_says_what_uses_each_item_or_not_used(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url, "/config#personas")
    implementer = page.locator('[data-scope="item:personas/implementer"]')
    expect(implementer.locator("[data-used-by]")).to_have_text(
        "Used by: Implement stage, symphony demo", timeout=_WAIT,
    )
    reviewer = page.locator('[data-scope="item:personas/reviewer"]')
    expect(reviewer.locator("[data-used-by]")).to_have_text("Used by: Not used")
    _tab(page, "Models")
    expect(page.locator('[data-scope="item:model_endpoints/qwen-coder"] [data-used-by]')).to_contain_text(
        "mode single-qwen",
    )


@pytest.mark.e2e
def test_the_symphonies_tab_is_read_only_and_links_to_each_symphony(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    _open_settings(page, live_server_url)
    _tab(page, "Symphonies")
    panel = _active_panel(page)
    expect(panel.locator("input, textarea, select")).to_have_count(0)
    expect(panel.locator("a", has_text="Edit settings")).to_have_attribute("href", "/symphonies/demo")
    expect(panel).to_contain_text("Edit this on the symphony's own page.")


# --- the Personas page links to Settings -----------------------------------------


@pytest.mark.e2e
def test_the_personas_link_opens_settings_on_that_persona(page: Page, live_server_url: str) -> None:
    _stub_config(page, _snapshot(), [])
    page.route("**/api/personas", lambda route: route.fulfill(
        status=200, content_type="application/json", headers={"ETag": '"persona-1"'},
        body=json.dumps([{"role": "implementer", "instructions": "Implement the change.", "is_default": True}]),
    ))
    page.goto(f"{live_server_url}/personas")
    page.locator("#personas-page-section a", has_text="Edit in Settings").click()
    expect(page).to_have_url(f"{live_server_url}/config#personas/implementer", timeout=_WAIT)
    expect(page.locator('[data-scope="item:personas/implementer"] textarea')).to_be_visible(timeout=_WAIT)

"""#225: the Settings follow-ups in a real browser, with the config API stubbed.

Clear is offered only on an optional secret (github_token is required under pat auth, so it
has none). A routing table that already holds a duplicate (backend, model) pair shows one
warning on the Routing tab that names the pair and its positions. A secret's Show toggle
reveals only what the reader typed, never the masked marker, and works from the keyboard.
Runs in Chromium and Firefox (--browser firefox).
"""
from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from coordinare.config_descriptors import SECRET_MASK

_WAIT = 5000
_TOKEN_TYPED = "ghp_TYPEDSECRET225"


@pytest.fixture(autouse=True)
def _close_without_the_leave_prompt(page: Page) -> Generator[None, None, None]:
    """Clear the leave prompt once a test is done, so Firefox never holds a dirty page open."""
    yield
    page.evaluate(
        "() => window.addEventListener('beforeunload', (event) => event.stopImmediatePropagation(), true)",
    )


def _secret(key: str, label: str, *, required: bool) -> dict[str, Any]:
    return {
        "key": key, "label": label, "help": None, "type": "secret", "current_value": SECRET_MASK,
        "default": None, "range": None, "enum": None, "editable": True, "restart_required": False,
        "secret": True, "required": required, "is_env_placeholder": False, "invalid": False,
        "section": "global", "store": "config_yaml",
    }


def _snapshot(*, routing: bool = False) -> dict[str, Any]:
    global_section = {
        "id": "global", "title": "General", "store": "config_yaml", "kind": "scalar_group",
        "description": "Settings shared by every symphony, unless a symphony overrides them.",
        "items": [], "invalid_banner": None,
        "settings": [
            _secret("global.github_token", "GitHub token", required=True),
            _secret("global.dashboard_auth_token", "Dashboard auth token", required=False),
        ],
    }
    routing_section = {
        "id": "routing", "title": "Routing", "store": "routing_yaml", "kind": "collection",
        "description": "Which base URL and wire format a self-hosted backend is sent to.",
        "settings": [], "items": [], "invalid_banner": None,
    }
    return {
        "sections": [global_section, routing_section],
        "content_hashes": {"config_yaml": "hash-1", "routing_yaml": "route-1"},
        "config_version": 0,
        "routing_available": routing,
        "routing_mounted": routing,
        "symphony_overrides": {},
    }


def _stub(page: Page, snapshot: dict[str, Any], entries: list[dict[str, Any]] | None = None) -> None:
    def all_config(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", headers={"ETag": '"hash-1"'},
                      body=json.dumps(snapshot))

    def personas(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", headers={"ETag": '"persona-1"'}, body="[]")

    def routing(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"routing_available": True, "entries": entries or [],
                                       "content_hash": "route-1"}))

    page.route("**/api/config/all", all_config)
    page.route("**/api/personas", personas)
    page.route("**/api/config/routing", routing)


def _open(page: Page, live_server_url: str) -> None:
    page.goto(f"{live_server_url}/config")
    expect(page.locator("#config-page-section [role=tab]").first).to_be_visible(timeout=_WAIT)


def _secret_input(page: Page, field: str) -> Any:
    return page.locator(f'#config-page-section [data-field="{field}"]')


def _toggle_for(page: Page, field: str) -> Any:
    return _secret_input(page, field).locator("xpath=..").locator('[data-cfg-action="toggle-secret"]')


def _entry(backend: str, model: str) -> dict[str, Any]:
    return {"backend": backend, "model": model, "used_by": [],
            "target": {"base_url": f"http://{backend}.local/v1", "wire_format": "openai", "strategy": "normalize"}}


# --- Clear is offered only on an optional secret ----------------------------------


@pytest.mark.e2e
def test_no_clear_on_github_token_but_clear_on_dashboard_auth_token(page: Page, live_server_url: str) -> None:
    _stub(page, _snapshot())
    _open(page, live_server_url)
    expect(_secret_input(page, "github_token")).to_be_visible(timeout=_WAIT)
    expect(page.locator('#config-page-section [data-clear-for="github_token"]')).to_have_count(0)
    expect(page.locator('#config-page-section [data-clear-for="dashboard_auth_token"]')).to_have_count(1)
    expect(page.locator('#config-page-section [data-clear-for="dashboard_auth_token"]')).to_have_text("Clear")


# --- the duplicate routing pairs warning -----------------------------------------


@pytest.mark.e2e
def test_duplicate_routing_pairs_show_one_warning_with_their_positions(page: Page, live_server_url: str) -> None:
    entries = [
        _entry("vllm", "qwen"), _entry("ollama", "llama"), _entry("vllm", "qwen"),
        _entry("vllm", "coder"), _entry("ollama", "llama"),
    ]
    _stub(page, _snapshot(routing=True), entries)
    _open(page, live_server_url)
    page.locator("#config-page-section [role=tab]", has_text="Routing").first.click()
    warning = page.locator("#config-page-section [data-routing-duplicates]")
    expect(warning).to_have_count(1, timeout=_WAIT)
    expect(warning).to_contain_text("duplicate backend and model pairs")
    expect(warning).to_contain_text("vllm")
    expect(warning).to_contain_text("entries 1, 3")
    expect(warning).to_contain_text("ollama")
    expect(warning).to_contain_text("entries 2, 5")
    expect(warning).to_contain_text("remove the others")
    expect(warning).not_to_contain_text("coder")


@pytest.mark.e2e
def test_no_duplicate_warning_when_every_pair_is_unique(page: Page, live_server_url: str) -> None:
    entries = [_entry("vllm", "qwen"), _entry("ollama", "llama")]
    _stub(page, _snapshot(routing=True), entries)
    _open(page, live_server_url)
    page.locator("#config-page-section [role=tab]", has_text="Routing").first.click()
    expect(page.locator("#config-page-section [data-routing-duplicates]")).to_have_count(0)
    expect(page.locator("#cfg-routing-entries [data-rentry]")).to_have_count(2, timeout=_WAIT)


# --- the Show toggle --------------------------------------------------------------


@pytest.mark.e2e
def test_show_reveals_typed_text_and_never_the_mask(page: Page, live_server_url: str) -> None:
    _stub(page, _snapshot())
    _open(page, live_server_url)
    field = _secret_input(page, "dashboard_auth_token")
    expect(field).to_have_attribute("type", "password", timeout=_WAIT)
    toggle = _toggle_for(page, "dashboard_auth_token")
    expect(toggle).to_have_attribute("aria-pressed", "false")

    # The stored value is the masked marker. Show must not reveal it as text.
    toggle.click()
    expect(field).to_have_attribute("type", "password")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text("stays hidden")
    assert field.input_value() == SECRET_MASK

    # What the reader types is revealed when Show is pressed, and hidden again on Hide.
    field.fill(_TOKEN_TYPED)
    toggle.click()
    expect(field).to_have_attribute("type", "text")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    expect(toggle).to_have_text("Hide")
    assert field.input_value() == _TOKEN_TYPED
    toggle.click()
    expect(field).to_have_attribute("type", "password")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(toggle).to_have_text("Show")


@pytest.mark.e2e
def test_show_toggle_works_from_the_keyboard(page: Page, live_server_url: str) -> None:
    _stub(page, _snapshot())
    _open(page, live_server_url)
    field = _secret_input(page, "dashboard_auth_token")
    expect(field).to_be_visible(timeout=_WAIT)
    field.fill(_TOKEN_TYPED)
    toggle = _toggle_for(page, "dashboard_auth_token")
    toggle.focus()
    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    expect(field).to_have_attribute("type", "text")
    toggle.focus()
    page.keyboard.press("Space")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(field).to_have_attribute("type", "password")


@pytest.mark.e2e
def test_the_mask_is_never_shown_on_github_token_either(page: Page, live_server_url: str) -> None:
    _stub(page, _snapshot())
    _open(page, live_server_url)
    field = _secret_input(page, "github_token")
    expect(field).to_have_attribute("type", "password", timeout=_WAIT)
    _toggle_for(page, "github_token").click()
    expect(field).to_have_attribute("type", "password")
    assert field.input_value() == SECRET_MASK

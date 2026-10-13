"""#126: the Config page in a real browser, with the config API stubbed."""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route, expect

_SNAPSHOT = {
    "sections": [{
        "id": "global", "title": "Global", "description": None, "store": "config_yaml",
        "kind": "scalar_group", "items": [], "invalid_banner": None,
        "settings": [
            {"key": "github_project_number", "label": "GitHub project number", "help": None, "type": "int",
             "current_value": 0, "default": None, "range": None, "enum": None, "editable": True,
             "restart_required": False, "secret": False, "is_env_placeholder": False, "invalid": False,
             "section": "global", "store": "config_yaml"},
            {"key": "max_feedback_cycles", "label": "Max feedback cycles", "help": None, "type": "int",
             "current_value": 50, "default": 5, "range": None, "enum": None, "editable": True,
             "restart_required": False, "secret": False, "is_env_placeholder": False, "invalid": False,
             "section": "global", "store": "config_yaml"},
        ],
    }],
    "content_hashes": {"config_yaml": "abc"},
    "config_version": 0,
    "routing_available": False,
    "routing_mounted": False,
    "symphony_overrides": {
        "github_project_number": [
            {"symphony": "placeholder", "value": "#5"}, {"symphony": "conductor-e2e", "value": "#16"},
        ],
    },
}


@pytest.mark.e2e
def test_config_page_names_overriding_symphonies_and_speaks_plainly(page: Page, live_server_url: str) -> None:
    def fulfil(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_SNAPSHOT))

    page.route("**/api/config/all", fulfil)
    page.goto(f"{live_server_url}/config")

    status = page.locator("#cfg-status-line")
    expect(status).to_contain_text("Loaded when the daemon started; not reloaded since.")
    expect(status).to_contain_text("No routing table is mounted on this host, so model routing uses defaults.")
    text = status.inner_text()
    assert "Config version" not in text and "routing unavailable" not in text

    notes = page.locator("[data-override-note]")
    expect(notes).to_have_count(1)
    expect(notes).to_contain_text("Overridden by symphonies placeholder (#5), conductor-e2e (#16)")
    link = notes.locator("a", has_text="conductor-e2e")
    expect(link).to_have_attribute("href", "/symphonies/conductor-e2e")
    # The note sits on the overridden field only.
    row = page.locator("[data-fieldrow]", has=page.locator("[data-override-note]"))
    expect(row).to_contain_text("GitHub project number")

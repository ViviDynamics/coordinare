"""#143 third check, in a browser against the REAL dashboard server.

Written before the fixes. The server here is the real app over a real config.yaml and routing.yaml
(no stubbed write path). A concurrent edit is made on disk after the page has loaded, so the save
that follows is refused by the server's own stale-version check, not by a stub.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import uvicorn
import yaml
from playwright.sync_api import Page, Route, expect

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app
from tests.e2e.conftest import mark_daemon_started
from tests.e2e.test_143_settings import _WAIT, _open_settings, _tab
from tests.unit.conftest import REPRESENTATIVE_CONFIG, REPRESENTATIVE_ROUTING


@pytest.fixture(autouse=True)
def _close_without_the_leave_prompt(page: Page) -> Generator[None, None, None]:
    yield
    page.evaluate(
        "() => window.addEventListener('beforeunload', (event) => event.stopImmediatePropagation(), true)",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def real_server(tmp_path: Path) -> Generator[dict[str, Any], None, None]:
    """The real dashboard app over a real config.yaml and routing.yaml, served on a free port."""
    routing_path = tmp_path / "routing.yaml"
    routing_path.write_text(yaml.safe_dump(REPRESENTATIVE_ROUTING, sort_keys=False))
    raw = {**REPRESENTATIVE_CONFIG, "github_token": "ghp_fixturetoken"}  # a literal: the placeholder fails validation on save
    raw["performer_endpoints"] = [{
        "id": "vllm-box", "mode": "ephemeral", "roles": ["implementer"], "image": "performer:full",
        "volumes": [{"host_path": str(routing_path), "container_path": "/devenv/routing.yaml", "mode": "ro"}],
        "env": {"SELFHOSTED_ROUTING_CONFIG": "/devenv/routing.yaml"},
    }]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    validated = CoordinareConfiguration(**coerce_multi_symphony_raw(
        {**raw, "github_token": "ghp_fixturetoken"}))
    daemon = MagicMock()
    daemon.state = {"coordinare_config": validated, "config_version": 1}
    # The same daemon surface the shared E2E mock sets. The live-update stream reads these, and an
    # unset one is a MagicMock that cannot be serialised into the snapshot.
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    mark_daemon_started(daemon)
    app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "config": config_path, "routing": routing_path}
    server.should_exit = True
    thread.join(timeout=5)


def _save_section(page: Page) -> None:
    page.locator('#config-page-section [data-section="global"] button', has_text="Save changes").click()
    page.locator("#config-page-section [data-review] button", has_text="Save these changes").click()


def _entry(backend: str, model: str, base: str) -> dict[str, Any]:
    return {"backend": backend, "model": model, "target": {
        "base_url": base, "wire_format": "openai", "strategy": "normalize",
        "normalizers": ["harmony_tool_calls"]}}


def _write_routing(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(yaml.safe_dump({"entries": entries}, sort_keys=False))


# --- Block 1: a real stale save offers Reload and reapply -----------------------------------


@pytest.mark.e2e
def test_r1_a_real_stale_section_save_offers_reload_and_reapply(page: Page, real_server: dict[str, Any]) -> None:
    _open_settings(page, real_server["url"])
    page.locator('[data-field="poll_interval_seconds"]').fill("45")
    # Someone else changes the file after this page loaded.
    text = real_server["config"].read_text()
    real_server["config"].write_text(text + "\n# edited elsewhere\n")
    _save_section(page)
    status = page.locator('#config-page-section [data-section="global"] [data-status]')
    expect(status).to_contain_text("Someone else changed", timeout=_WAIT)
    btn = page.locator('#config-page-section [data-section="global"] [data-status] button', has_text="Reload and reapply")
    assert btn.count() == 1, "R1 BLOCK: the real stale-version refusal offers no Reload and reapply"


@pytest.mark.e2e
def test_r2_a_real_stale_routing_save_offers_reload_and_reapply(page: Page, real_server: dict[str, Any]) -> None:
    _write_routing(real_server["routing"], [_entry("vllm", "qwen2.5-coder", "http://a"),
                                             _entry("vllm", "other", "http://b")])
    _open_settings(page, real_server["url"])
    _tab(page, "Routing")
    entry = page.locator("#cfg-routing-entries [data-rentry]").nth(0)
    expect(entry).to_be_visible(timeout=_WAIT)
    entry.locator('[data-rkey="target.base_url"]').fill("http://typed")
    # The table changes on disk after the page loaded: the entry's base URL, not its identity.
    _write_routing(real_server["routing"], [_entry("vllm", "qwen2.5-coder", "http://elsewhere"),
                                             _entry("vllm", "other", "http://b")])
    entry.locator('button[data-cfg-action="save-routing"]').click()
    page.locator("#cfg-routing-entries [data-review] button", has_text="Save these changes").click()
    expect(entry.locator("[data-status]")).to_contain_text("changed", timeout=_WAIT)
    assert entry.locator("[data-status] button", has_text="Reload and reapply").count() == 1, (
        "R2 BLOCK: a real stale routing save offers no Reload and reapply")


# --- Block 2: duplicate pairs keep each entry's unsaved edits apart -------------------------


@pytest.mark.e2e
def test_r3_duplicate_pairs_keep_each_entrys_unsaved_edit_on_its_own_entry(page: Page, real_server: dict[str, Any]) -> None:
    _write_routing(real_server["routing"], [
        _entry("A", "a", "http://one"), _entry("A", "a", "http://two"), _entry("B", "b", "http://three"),
    ])
    _open_settings(page, real_server["url"])
    _tab(page, "Routing")
    blocks = page.locator("#cfg-routing-entries [data-rentry]")
    expect(blocks).to_have_count(3, timeout=_WAIT)
    blocks.nth(0).locator('[data-rkey="target.base_url"]').fill("http://X-zero")
    blocks.nth(1).locator('[data-rkey="target.base_url"]').fill("http://Y-one")
    blocks.nth(2).locator('[data-rkey="model"]').fill("b-saved")
    blocks.nth(2).locator('button[data-cfg-action="save-routing"]').click()
    page.locator("#cfg-routing-entries [data-review] button", has_text="Save these changes").click()
    expect(page.locator("#cfg-routing-entries [data-rentry]", has_text="B / b-saved")).to_be_visible(timeout=_WAIT)
    b0 = page.locator("#cfg-routing-entries [data-rentry]").nth(0).locator('[data-rkey="target.base_url"]').input_value()
    b1 = page.locator("#cfg-routing-entries [data-rentry]").nth(1).locator('[data-rkey="target.base_url"]').input_value()
    assert (b0, b1) == ("http://X-zero", "http://Y-one"), "R3 BLOCK: duplicate-pair edits were swapped or merged"


# --- Reapplied edits keep their clear and the clear is named again -------------------------


@pytest.mark.e2e
def test_r4_a_reapplied_clear_after_a_real_conflict_is_still_a_clear(page: Page, real_server: dict[str, Any]) -> None:
    _open_settings(page, real_server["url"])
    page.locator('[data-clear-for="dashboard_auth_token"]').click()
    page.locator('[data-field="poll_interval_seconds"]').fill("45")
    text = real_server["config"].read_text()
    real_server["config"].write_text(text + "\n# edited elsewhere\n")
    _save_section(page)
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text(
        "Someone else changed", timeout=_WAIT)
    page.locator('#config-page-section [data-section="global"] [data-status] button', has_text="Reload and reapply").click()
    token = page.locator('#config-page-section [data-field="dashboard_auth_token"]')
    expect(token).to_have_attribute("data-clear", "true", timeout=_WAIT)
    expect(token).to_have_value("")


# --- Typing made during the list refresh is kept ----------------------------------------


@pytest.mark.e2e
def test_r5_typing_during_a_routing_refresh_is_kept(page: Page, real_server: dict[str, Any]) -> None:
    _write_routing(real_server["routing"], [_entry("A", "a", "http://a"), _entry("B", "b", "http://b"),
                                             _entry("C", "c", "http://c")])
    _open_settings(page, real_server["url"])
    _tab(page, "Routing")
    blocks = page.locator("#cfg-routing-entries [data-rentry]")
    expect(blocks).to_have_count(3, timeout=_WAIT)
    # Hold the list request the page makes after the delete, so the reader can type while it is in flight.
    held: list[Route] = []
    gate = {"on": False}

    def hold_refresh(route: Route) -> None:
        if gate["on"]:
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/config/routing", hold_refresh)
    gate["on"] = True
    blocks.nth(0).locator('button[data-cfg-action="delete-routing"]').click()
    page.locator("#cfg-routing-entries [data-review] button", has_text="Save these changes").click()
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(100)
    assert held, "the refresh after the delete was not requested"
    # The old list is still on screen: the reader types into what was entry B.
    blocks.nth(1).locator('[data-rkey="model"]').fill("typed-during-refresh")
    held[0].continue_()
    kept = page.locator("#cfg-routing-entries [data-rentry]", has_text="B / b").locator('[data-rkey="model"]')
    expect(kept).to_have_value("typed-during-refresh", timeout=_WAIT)


# --- Stub-free: the secret input is cleared and the placeholder is reset after a real save -----


@pytest.mark.e2e
def test_r6_placeholder_resets_after_a_real_clear_is_saved(page: Page, real_server: dict[str, Any]) -> None:
    _open_settings(page, real_server["url"])
    token = page.locator('#config-page-section [data-field="dashboard_auth_token"]')
    page.locator('[data-clear-for="dashboard_auth_token"]').click()
    expect(token).to_have_attribute("placeholder", "Will be cleared when you save")
    page.locator('[data-field="poll_interval_seconds"]').fill("45")
    _save_section(page)
    expect(page.locator('#config-page-section [data-section="global"] [data-status]')).to_contain_text(
        "Saved", timeout=_WAIT)
    expect(token).not_to_have_attribute("placeholder", "Will be cleared when you save")

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.test_143_settings import _snapshot, _stub_config

_SOURCE = '[data-scope="item:personas/implementer"] textarea'
_REVIEW = '[data-scope="item:personas/implementer"] [data-review]'
_TEXT = "  # Instructions\r\n\r\nUse **care** and `code`.  \r\n"
_WIDE = {"width": 1280, "height": 800}
_NARROW = {"width": 375, "height": 800}
_MIN_EDITOR_FRACTION = 0.6


def _open(page: Page, url: str, *, conflict: bool = False) -> list[dict[str, Any]]:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _snapshot(), saves)
    state = {"text": _TEXT, "version": '"v1"'}

    def personas(route: Route) -> None:
        route.fulfill(json=[{"role": "implementer", "instructions": state["text"], "is_default": False},
                            {"role": "reviewer", "instructions": "# Review\nRead carefully", "is_default": True}],
                      headers={"ETag": state["version"]})

    def save(route: Route) -> None:
        if route.request.method == "GET":
            route.fulfill(json={"versions": []})
            return
        saves.append({"body": json.loads(route.request.post_data or "{}"), "headers": route.request.headers})
        if conflict:
            state["text"] = "# Colleague's text"
            state["version"] = '"v2"'
            route.fulfill(status=409, json={"conflict": True, "error": "changed"})
        else:
            state["text"] = saves[-1]["body"].get("instructions", "# Built in")
            state["version"] = '"v2"'
            route.fulfill(json={"instructions": state["text"], "is_default": False}, headers={"ETag": state["version"]})

    page.route("**/api/personas", personas)
    page.route("**/api/personas/*", save)
    page.goto(url)
    return saves


@pytest.mark.e2e
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_one_persona_keyboard_tools_and_responsive_preview(page: Page, live_server_url: str, theme: str) -> None:
    page.set_viewport_size(_WIDE)
    _open(page, f"{live_server_url}/config#personas/implementer")
    page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", theme)
    source = page.locator(_SOURCE)
    expect(source).to_be_visible()
    bounds = source.bounding_box()
    assert bounds is not None and bounds["height"] >= _WIDE["height"] * _MIN_EDITOR_FRACTION
    expect(page.locator('[data-persona-panel]:visible')).to_have_count(1)
    expect(page.locator('[data-md-preview]:visible h1')).to_have_text("Instructions")
    expect(page.locator('[data-md-highlight]:visible .md-heading')).to_have_count(1)
    source.fill("abc")
    source.press("Home")
    source.press("Tab")
    expect(source).to_have_value("  abc")
    source.press("Home")
    source.press("Shift+Tab")
    expect(source).to_have_value("abc")
    source.press("Tab")
    expect(source).to_have_value("  abc")
    source.press("Escape")
    source.press("Tab")
    expect(source).not_to_be_focused()
    expect(page.locator('[data-persona-tab="implementer"]')).to_contain_text("Unsaved")
    page.locator('[data-persona-tab="implementer"]').focus()
    page.keyboard.press("ArrowRight")
    expect(page).to_have_url(f"{live_server_url}/config#personas/reviewer")
    page.keyboard.press("ArrowLeft")
    expect(source).to_have_value("  abc")
    page.set_viewport_size(_NARROW)
    page.locator('[data-persona-panel]:visible [data-md-view="preview"]').click()
    expect(source).not_to_be_visible()
    expect(page.locator('[data-md-preview]:visible')).to_contain_text("abc")
    page.locator('[data-persona-panel]:visible [data-md-view="write"]').click()
    expect(source).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.e2e
def test_preview_cannot_execute_persona_markup(page: Page, live_server_url: str) -> None:
    _open(page, f"{live_server_url}/config#personas/implementer")
    source = page.locator(_SOURCE)
    source.fill('<script>window.personaPwned=true</script>\n<img src=x onerror="window.personaPwned=true">\n[bad](javascript:window.personaPwned=true)\n<svg onload="window.personaPwned=true"></svg>')
    preview = page.locator('[data-persona-panel]:visible [data-md-preview]')
    expect(preview).to_contain_text("<script>")
    expect(preview.locator("script, img, svg, [onerror], [onload], a")).to_have_count(0)
    assert page.evaluate("window.personaPwned === undefined")


@pytest.mark.e2e
def test_exact_reviewed_source_reset_and_unsaved_guard(page: Page, live_server_url: str) -> None:
    saves = _open(page, f"{live_server_url}/config#personas/implementer")
    source = page.locator(_SOURCE)
    source.fill("  # Edited\n\nKeep trailing spaces.  \n")
    source.press("ControlOrMeta+s")
    review = page.locator(_REVIEW)
    expect(review).to_contain_text("Keep trailing spaces.")
    expect(source).to_be_disabled()
    assert saves == []
    review.get_by_role("button", name="Save these changes").click()
    expect(page.locator('[data-persona-panel]:visible [data-status]')).to_have_text("Saved")
    assert saves[-1]["body"]["instructions"] == "  # Edited\n\nKeep trailing spaces.  \n"
    assert saves[-1]["headers"]["if-match"] == '"v1"'
    source.fill("unsaved")
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator('a.nav-link[href="/personas"]').click()
    expect(source).to_have_value("unsaved")
    page.locator('[data-persona-panel]:visible button', has_text="Reset to default").click()
    expect(review).to_contain_text("built-in default")
    expect(review).to_contain_text("discarded")
    review.get_by_role("button", name="Keep editing").click()
    expect(source).to_have_value("unsaved")


@pytest.mark.e2e
def test_conflict_reapply_keeps_other_persona_drafts(page: Page, live_server_url: str) -> None:
    saves = _open(page, f"{live_server_url}/config#personas/implementer", conflict=True)
    source = page.locator(_SOURCE)
    source.fill("my draft  \n")
    page.locator('[data-persona-tab="reviewer"]').click()
    page.locator('[data-persona-panel]:visible textarea').fill("review draft")
    page.locator('[data-persona-tab="implementer"]').click()
    source.press("ControlOrMeta+s")
    page.locator(_REVIEW).get_by_role("button", name="Save these changes").click()
    page.get_by_role("button", name="Reload and reapply").click()
    expect(source).to_have_value("my draft  \n")
    page.locator('[data-persona-tab="reviewer"]').click()
    expect(page.locator('[data-persona-panel]:visible textarea')).to_have_value("review draft")
    assert saves[-1]["headers"]["if-match"] == '"v1"'


@pytest.mark.e2e
def test_read_only_page_selection_survives_reload_and_links_to_editor(page: Page, live_server_url: str) -> None:
    page.set_viewport_size(_NARROW)
    _open(page, f"{live_server_url}/personas#reviewer")
    expect(page.locator('[data-persona-panel]:visible h1')).to_have_text("Review")
    expect(page.locator('#personas-page textarea, #personas-page [data-md-tool]')).to_have_count(0)
    page.locator('[data-persona-tab="implementer"]').click()
    expect(page.locator('[data-persona-panel]:visible')).to_contain_text("Custom text")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.reload()
    expect(page.locator('[data-persona-panel]:visible h1')).to_have_text("Instructions")
    page.locator('[data-persona-panel]:visible').get_by_role("link", name="Edit in Settings").click()
    expect(page.locator(_SOURCE)).to_be_focused()


@pytest.mark.e2e
def test_untouched_source_does_not_write_normalized_text(page: Page, live_server_url: str) -> None:
    saves = _open(page, f"{live_server_url}/config#personas/implementer")
    source = page.locator(_SOURCE)
    expect(source).to_have_value(_TEXT.replace("\r\n", "\n"))
    source.press("ControlOrMeta+s")
    expect(page.locator('[data-persona-panel]:visible [data-status]')).to_have_text("No changes to save")
    expect(page.locator('[data-persona-tab="implementer"] [data-persona-dirty]')).to_be_hidden()
    assert saves == []


@pytest.mark.e2e
def test_reset_preserves_another_personas_draft(page: Page, live_server_url: str) -> None:
    saves = _open(page, f"{live_server_url}/config#personas/implementer")
    source = page.locator(_SOURCE)
    source.fill("discard this")
    page.locator('[data-persona-tab="reviewer"]').click()
    page.locator('[data-persona-panel]:visible textarea').fill("keep this")
    page.locator('[data-persona-tab="implementer"]').click()
    page.locator('[data-persona-panel]:visible').get_by_role("button", name="Reset to default").click()
    assert saves == []
    page.locator(_REVIEW).get_by_role("button", name="Save these changes").click()
    expect(source).to_have_value("# Built in")
    page.locator('[data-persona-tab="reviewer"]').click()
    expect(page.locator('[data-persona-panel]:visible textarea')).to_have_value("keep this")

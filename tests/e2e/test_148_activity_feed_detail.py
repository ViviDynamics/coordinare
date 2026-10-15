"""148: the activity feed says what ran, and its kinds can be shown or hidden.

A tool row shows the command or path it ran, narration reads as prose in its
own row, a lifecycle event shows with the Tools kind collapsed, and the kind
chips are remembered per browser. A hostile command reaches the page as text.
"""
from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Dialog, Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(90)]

_CARD = "PVTI_148"
_GH_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_TOOLS_CHIP = "#activity-feed-card .af-chip[data-chip='tools']"
_LIFECYCLE_CHIP = "#activity-feed-card .af-chip[data-chip='lifecycle']"


def _entry(seq: int, **extra: Any) -> dict[str, Any]:
    base = {
        "seq": seq, "timestamp": "2026-10-13T09:00:00+00:00", "card_id": _CARD, "card_number": 148,
        "card_title": "Activity detail", "stage": "implementing", "activity_type": "progress",
        "text": f"entry {seq}", "summary": "", "status": "", "truncated": False,
        "session_id": "", "performer_id": "", "is_delta": False, "stream_id": "",
    }
    base.update(extra)
    return base


def _open(page: Page, live_server_url: str) -> None:
    page.goto(live_server_url)
    # The first state_update starts the activity run (afSyncRun) and clears the feed, so an
    # append that lands before it is wiped. #phase is rendered by the server, so it is no signal.
    page.wait_for_function('() => typeof _afRunId === "string" && _afRunId.length > 0', timeout=_WAIT_SSE)


def _append(page: Page, entries: list[dict[str, Any]]) -> None:
    page.evaluate("entries => afAppend(entries)", entries)


def test_a_tool_row_shows_the_command_it_ran(page: Page, live_server_url: str) -> None:
    _open(page, live_server_url)
    _append(page, [_entry(9101, activity_type="tool_use", text="Bash",
                         summary="Bash · uv run pytest -q", session_id="", performer_id="")])
    page.locator(_TOOLS_CHIP).click()
    row = page.locator("#activity-feed .af-row-tools")
    expect(row).to_have_count(1)
    expect(row.locator("pre.af-tool")).to_contain_text("Bash · uv run pytest -q")


def test_narration_and_a_tool_call_look_different(page: Page, live_server_url: str) -> None:
    _open(page, live_server_url)
    _append(page, [
        _entry(9201, activity_type="progress", text="The tests already pass on this branch."),
        _entry(9202, activity_type="tool_use", text="Read", summary="Read · src/foo.py"),
    ])
    page.locator(_TOOLS_CHIP).click()
    narration = page.locator("#activity-feed .af-row-agent")
    expect(narration).to_have_count(1)
    expect(narration.locator(".af-narration")).to_contain_text("The tests already pass on this branch.")
    tool = page.locator("#activity-feed .af-row-tools")
    expect(tool.locator("pre.af-tool")).to_contain_text("Read · src/foo.py")
    # The two read differently: a tool call is code, narration is prose.
    assert narration.locator("pre").count() == 0
    assert tool.locator("p.af-narration").count() == 0


def test_lifecycle_is_visible_without_expanding_anything_while_tools_are_collapsed(
    page: Page, live_server_url: str,
) -> None:
    _open(page, live_server_url)
    _append(page, [
        _entry(9301, activity_type="tool_use", text="Bash", summary="Bash · make test"),
        _entry(9302, activity_type="stage_change", text="implementing to reviewing"),
    ])
    expect(page.locator(_TOOLS_CHIP)).to_have_attribute("aria-pressed", "false")
    expect(page.locator(_LIFECYCLE_CHIP)).to_have_attribute("aria-pressed", "true")
    lifecycle = page.locator("#activity-feed .af-row-lifecycle")
    expect(lifecycle).to_have_count(1)
    expect(lifecycle).to_be_visible()
    expect(lifecycle).to_contain_text("implementing to reviewing")
    # A plain row is never a collapsed group, so nothing has to be opened to read it.
    assert lifecycle.locator("details").count() == 0
    expect(page.locator("#activity-feed .af-row-tools")).to_have_count(0)


def test_a_failed_tool_result_still_shows_under_errors_while_tools_are_collapsed(
    page: Page, live_server_url: str,
) -> None:
    _open(page, live_server_url)
    _append(page, [_entry(9401, activity_type="tool_result", summary="result error",
                         status="error", text="exit 2: tests failed")])
    expect(page.locator("#activity-feed .af-result-error")).to_contain_text("exit 2: tests failed")


def test_the_chips_persist_across_a_reload_in_this_browser(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url)
    # Recorded on the daemon, so the reload brings it back through the SSE backfill.
    store.activity_log.record(
        activity_type="tool_use", card_id=_CARD, card_number=148, card_title="Activity detail",
        stage="implementing", text="Bash", summary="Bash · pnpm lint",
    )
    expect(page.locator("#activity-feed")).not_to_contain_text("pnpm lint", timeout=_WAIT_SSE)
    page.locator(_TOOLS_CHIP).click()
    expect(page.locator(_TOOLS_CHIP)).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#activity-feed")).to_contain_text("Bash · pnpm lint")

    page.locator(_LIFECYCLE_CHIP).click()
    expect(page.locator(_LIFECYCLE_CHIP)).to_have_attribute("aria-pressed", "false")

    page.reload()
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    expect(page.locator(_TOOLS_CHIP)).to_have_attribute("aria-pressed", "true", timeout=_WAIT_LIVE)
    expect(page.locator(_LIFECYCLE_CHIP)).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#activity-feed")).to_contain_text("Bash · pnpm lint", timeout=_WAIT_LIVE)


def test_the_default_chips_are_lifecycle_agent_and_errors(page: Page, live_server_url: str) -> None:
    _open(page, live_server_url)
    for chip, pressed in [("lifecycle", "true"), ("agent", "true"), ("tools", "false"), ("errors", "true")]:
        expect(page.locator(f"#activity-feed-card .af-chip[data-chip='{chip}']")).to_have_attribute(
            "aria-pressed", pressed,
        )


def test_a_hostile_command_reaches_the_page_as_text_and_runs_nothing(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    dialogs: list[str] = []

    def record(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", record)
    _open(page, live_server_url)
    hostile = "Bash · echo <img src=x onerror=alert(1)> </pre><script>alert(2)</script>"
    store.activity_log.record(
        activity_type="tool_use", card_id=_CARD, card_number=148, card_title="Activity detail",
        stage="implementing", text="Bash", summary=hostile,
    )
    page.locator(_TOOLS_CHIP).click()
    expect(page.locator("#activity-feed")).to_contain_text("echo <img src=x onerror=alert(1)>", timeout=_WAIT_LIVE)
    assert page.locator("#activity-feed img").count() == 0
    assert page.locator("#activity-feed script").count() == 0
    assert dialogs == []


def test_a_secret_in_a_command_never_reaches_the_page(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open(page, live_server_url)
    store.activity_log.record(
        activity_type="tool_use", card_id=_CARD, card_number=148, card_title="Activity detail",
        stage="implementing", text="Bash", summary=f"Bash · GITHUB_TOKEN={_GH_TOKEN} gh pr list",
    )
    page.locator(_TOOLS_CHIP).click()
    expect(page.locator("#activity-feed")).to_contain_text("gh pr list", timeout=_WAIT_LIVE)
    assert _GH_TOKEN not in page.locator("#activity-feed").inner_text()
    assert _GH_TOKEN not in page.content()

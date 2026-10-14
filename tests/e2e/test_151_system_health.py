"""#151: the System health card in a real browser.

The card is one line while every monitored subsystem is healthy, and opens by itself
when one is not. Each row says its status in words and how fresh that status is.
These tests push snapshots through the same SSE stream the daemon uses.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot

_BOARD_REASON = "last board check failed (TransientGitHubError)"
_WEBHOOK_REASON = "coordinare polls the board on a timer (webhooks.enabled is false), so GitHub changes are seen on the next check"


def _ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _row(key: str, name: str, status: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key, "name": name, "status": status, "summary": f"{name} summary",
        "reason": None, "fresh_label": "last checked", "checked_at": None, "last_success_at": None,
    }
    row.update(extra)
    return row


def _all_green_rows() -> list[dict[str, Any]]:
    return [
        _row("github_api", "GitHub API", "healthy", summary="4,800 of 5,000 requests left, resets 14:02 UTC",
             fresh_label="last call", checked_at=_ago(5), last_success_at=_ago(5)),
        _row("github_webhooks", "GitHub webhooks", "healthy", summary="deliveries are arriving",
             fresh_label="last delivery", checked_at=_ago(20), last_success_at=_ago(20)),
        _row("board_check", "Board check", "healthy", summary="reads the board every 30s",
             checked_at=_ago(8), last_success_at=_ago(8)),
        _row("performer_runtime", "Performer runtime", "healthy", summary="Docker · 1 performer in flight",
             fresh_label="last start", checked_at=_ago(30), last_success_at=_ago(30)),
        _row("model_backends", "Model backends", "healthy", summary="0 of last 3 turns ended in a system error",
             fresh_label="last turn", checked_at=_ago(60), last_success_at=_ago(60)),
        _row("disk_env_cache", "Disk and env cache", "healthy", summary="62% of disk free; no env cache in use",
             fresh_label="measured", checked_at=_ago(0)),
    ]


def _with_system_health(rows: list[dict[str, Any]], overall: str) -> dict[str, Any]:
    return _full_snapshot(system_health={"overall": overall, "rows": rows})


def _open_idle(page: Page, live_server_url: str) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_contain_text("Idle", timeout=_WAIT_SSE)


@pytest.mark.e2e
def test_all_green_collapses_to_one_line(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_idle(page, live_server_url)
    store.broadcaster.broadcast(_with_system_health(_all_green_rows(), "healthy"))

    card = page.locator("#subsystems-section")
    expect(card.locator("summary")).to_contain_text("All systems healthy", timeout=_WAIT_LIVE)
    expect(card.locator("details")).not_to_have_attribute("open", "")
    # The rows are inside the closed details, so none of them is on screen.
    expect(card.locator(".sh-row").first).not_to_be_visible()
    expect(card.locator(".sh-row")).to_have_count(6)


@pytest.mark.e2e
def test_one_degraded_subsystem_opens_the_card_and_is_visible_without_a_click(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_idle(page, live_server_url)
    rows = _all_green_rows()
    rows[2] = _row(
        "board_check", "Board check", "degraded", summary="1 failed in a row",
        checked_at=_ago(4), last_success_at=_ago(95), reason=_BOARD_REASON,
    )
    store.broadcaster.broadcast(_with_system_health(rows, "degraded"))

    card = page.locator("#subsystems-section")
    expect(card.locator("details")).to_have_attribute("open", "", timeout=_WAIT_LIVE)
    expect(card.locator("summary")).to_contain_text("1 system needs attention: Board check (degraded)")
    degraded = card.locator(".sh-row.sh-degraded")
    expect(degraded).to_be_visible()
    expect(degraded).to_contain_text("Board check")
    expect(degraded).to_contain_text("degraded")
    expect(degraded.locator(".sh-reason")).to_have_text(_BOARD_REASON)


@pytest.mark.e2e
def test_each_row_shows_how_fresh_its_status_is(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_idle(page, live_server_url)
    rows = _all_green_rows()
    rows[0] = _row(
        "github_api", "GitHub API", "degraded", summary="rate limit nearly used up",
        fresh_label="last call", checked_at=_ago(125), last_success_at=_ago(125), reason="rate limit nearly used up",
    )
    store.broadcaster.broadcast(_with_system_health(rows, "degraded"))

    card = page.locator("#subsystems-section")
    expect(card.locator(".sh-row").first).to_be_visible(timeout=_WAIT_LIVE)
    github = card.locator(".sh-row").first
    expect(github.locator(".sh-fresh").first).to_have_text(re.compile(r"^last call 2m \d+s ago$"))
    expect(github.locator(".sh-fresh").last).to_have_text(re.compile(r"^last success 2m \d+s ago$"))
    # The freshness is a live readout, so it keeps counting rather than freezing at the render.
    expect(github.locator(".sh-fresh").first).to_have_attribute("data-elapsed-since", re.compile(r"^\d{4}-"))
    board = card.locator(".sh-row").nth(2)
    expect(board.locator(".sh-fresh").first).to_have_text(re.compile(r"^last checked \d+s ago$"))


@pytest.mark.e2e
def test_a_subsystem_that_is_not_monitored_is_named_and_never_shown_green(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_idle(page, live_server_url)
    rows = _all_green_rows()
    rows[1] = _row(
        "github_webhooks", "GitHub webhooks", "not_monitored", summary="not in use", reason=_WEBHOOK_REASON,
    )
    store.broadcaster.broadcast(_with_system_health(rows, "healthy"))

    card = page.locator("#subsystems-section")
    summary = card.locator("summary")
    expect(summary).to_contain_text("All monitored systems healthy · 1 not monitored", timeout=_WAIT_LIVE)
    # The one-line summary is a neutral tone, not the healthy tone.
    expect(summary).to_have_class(re.compile(r"sh-tone-note"))
    expect(summary).not_to_have_class(re.compile(r"sh-tone-healthy"))

    # Open the card: the webhooks row says why it is not monitored, in words, not as green.
    summary.click()
    webhooks = card.locator(".sh-row").nth(1)
    expect(webhooks).to_be_visible()
    expect(webhooks).to_contain_text("not monitored")
    expect(webhooks.locator(".sh-reason")).to_have_text(_WEBHOOK_REASON)
    expect(webhooks.locator(".sh-fresh")).to_have_count(0)


@pytest.mark.e2e
def test_rows_awaiting_a_first_check_are_counted_on_a_closed_card(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    """Unknown rows are not problems: they neither open the card nor read as healthy."""
    _open_idle(page, live_server_url)
    rows = _all_green_rows()
    rows[2] = _row("board_check", "Board check", "unknown", summary="no board check yet",
                   reason="nothing observed since this daemon started")
    store.broadcaster.broadcast(_with_system_health(rows, "unknown"))

    card = page.locator("#subsystems-section")
    summary = card.locator("summary")
    expect(summary).to_contain_text("No problems seen · 1 awaiting first check", timeout=_WAIT_LIVE)
    expect(card.locator("details")).not_to_have_attribute("open", "")
    expect(summary).to_have_class(re.compile(r"sh-tone-note"))


@pytest.mark.e2e
def test_a_stale_performer_row_is_counted_neutrally_and_does_not_open_the_card(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_idle(page, live_server_url)
    rows = _all_green_rows()
    rows[3] = _row("performer_runtime", "Performer runtime", "stale", summary="Docker · 0 performers in flight",
                   fresh_label="last start", checked_at=_ago(7200), last_success_at=_ago(7200),
                   reason="no performer is running, and the last start is older than the freshness threshold")
    store.broadcaster.broadcast(_with_system_health(rows, "unknown"))

    card = page.locator("#subsystems-section")
    expect(card.locator("summary")).to_contain_text("No problems seen · 1 stale", timeout=_WAIT_LIVE)
    expect(card.locator("details")).not_to_have_attribute("open", "")
    expect(card.locator(".sh-row.sh-stale").first).to_contain_text("stale")


@pytest.mark.e2e
def test_a_card_the_user_expanded_stays_expanded_across_a_re_render(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    """The user can expand a card that is healthy and collapsed; the choice holds across same-status re-renders."""
    _open_idle(page, live_server_url)
    store.broadcaster.broadcast(_with_system_health(_all_green_rows(), "healthy"))
    card = page.locator("#subsystems-section")
    expect(card.locator("summary")).to_contain_text("All systems healthy", timeout=_WAIT_LIVE)
    expect(card.locator("details")).not_to_have_attribute("open", "")

    card.locator("summary").click()
    expect(card.locator("details")).to_have_attribute("open", "")
    expect(card.locator(".sh-row").first).to_be_visible()

    # A fresh snapshot with the same overall status re-renders the card; the user's expand holds.
    rows = _all_green_rows()
    rows[0] = _row("github_api", "GitHub API", "healthy", summary="4,700 of 5,000 requests left",
                   fresh_label="last call", checked_at=_ago(3), last_success_at=_ago(3))
    store.broadcaster.broadcast(_with_system_health(rows, "healthy"))
    expect(card.locator(".sh-row").first).to_contain_text("4,700 of 5,000", timeout=_WAIT_LIVE)
    expect(card.locator("details")).to_have_attribute("open", "")


@pytest.mark.e2e
def test_a_card_the_user_opened_stays_open_across_a_re_render_until_the_status_changes(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open_idle(page, live_server_url)
    degraded_rows = _all_green_rows()
    degraded_rows[2] = _row("board_check", "Board check", "degraded", summary="1 failed in a row",
                            checked_at=_ago(4), reason=_BOARD_REASON)
    store.broadcaster.broadcast(_with_system_health(degraded_rows, "degraded"))
    card = page.locator("#subsystems-section")
    expect(card.locator("details")).to_have_attribute("open", "", timeout=_WAIT_LIVE)

    # The user closes it. A fresh snapshot with the same status re-renders the card.
    card.locator("summary").click()
    expect(card.locator("details")).not_to_have_attribute("open", "")
    degraded_rows[2] = _row("board_check", "Board check", "degraded", summary="2 failed in a row",
                            checked_at=_ago(2), reason=_BOARD_REASON)
    store.broadcaster.broadcast(_with_system_health(degraded_rows, "degraded"))
    expect(card.locator(".sh-row").nth(2)).to_contain_text("2 failed in a row", timeout=_WAIT_LIVE)
    expect(card.locator("details")).not_to_have_attribute("open", "")

    # The overall status changes to healthy, which drops the choice: a healthy card is closed by default.
    store.broadcaster.broadcast(_with_system_health(_all_green_rows(), "healthy"))
    expect(card.locator("summary")).to_contain_text("All systems healthy", timeout=_WAIT_LIVE)
    expect(card.locator("details")).not_to_have_attribute("open", "")

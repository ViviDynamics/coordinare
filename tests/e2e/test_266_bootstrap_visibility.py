"""266: the env bootstrap is visible on the dashboard, checked in a real browser.

The Symphonies page says the bootstrap's state in words and keeps its recent attempts; the Dashboard status
strip names a bootstrap that is running or failing; a card the bootstrap holds says why; and the System
health row gives the reason a bootstrap failed. The snapshots go through the same SSE stream the daemon uses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot

_TABLE = "#symphonies-page-section"
_GEMFILE = "verify.sh non-zero: Could not locate Gemfile"
_BOOT_BOARD = "boot-sym"


def _ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _boot(**overrides: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "state": "not_started", "label": "Not started", "attempt": None, "max_attempts": 3,
        "commit": None, "started_at": None, "elapsed_seconds": None, "next_attempt_at": None,
        "ready_since": None, "reason": None, "reason_tail": [], "reset_hint": None, "backend": None,
        "model": None, "performer_id": None, "pod": None, "job_id": None, "infra_failures": 0, "history": [],
    }
    view.update(overrides)
    return view


def _symphony(name: str, boot: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name, "priority": 0, "github_project_number": 16, "github_org": "acme",
        "repo": f"{name}-repo", "enabled": True, "poll_interval_seconds": 30,
        "daemon_cycles_completed": 3, "cycle_count": 3, "error_count": 0,
        "last_poll_at": _ago(60), "env_bootstrap_performer_id": "perf",
        "env_status": {"state": "bootstrapping", "reason": "An env bootstrap is running."},
        "env_bootstrap": boot,
        "cards_active": 0, "cards_queued": 0, "cards_paused": 0,
        "spend_week": {"tracked": False, "reason": "Spend is not recorded.", "tokens": None, "cost": None},
        "state": {
            "board_snapshot": None, "board_titles": {}, "board_issue_numbers": {}, "board_issue_urls": {},
            "board_pr_urls": {}, "session_skip_reasons": {}, "todo_waits": {}, "todo_capacity": None,
            "active_sessions": {}, "last_error": None, "active_card": None,
        },
    }
    entry.update(overrides)
    return entry


def _open(page: Page, url: str, store: DashboardStore, symphonies: list[dict[str, Any]]) -> None:
    page.goto(url)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle", symphonies=symphonies))


def _env_cell(page: Page, name: str) -> Any:
    return page.locator(f"tr.symphony-row[data-href='/symphonies/{name}'] td").nth(4)


@pytest.mark.e2e
def test_a_running_bootstrap_says_its_attempt_and_start_on_the_symphonies_page(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    boot = _boot(
        state="bootstrapping", label="Bootstrapping, run 2 of 3", attempt=2,
        started_at=_ago(90), elapsed_seconds=90, backend="claude_code", model="sonnet", pod="pod-abc",
    )
    _open(page, live_server_url + "/symphonies", store, [_symphony("alpha-sym", boot)])

    cell = _env_cell(page, "alpha-sym")
    expect(cell).to_contain_text("Bootstrapping", timeout=_WAIT_LIVE)
    expect(cell).to_contain_text("Bootstrapping, run 2 of 3", timeout=_WAIT_LIVE)
    expect(cell).to_contain_text("claude_code sonnet", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_failed_bootstrap_shows_the_failing_line_and_its_history(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    history = [{
        "number": 2, "attempt": 1, "commit": "abc1234", "started_at": _ago(3600), "ended_at": _ago(3000),
        "duration_seconds": 600, "outcome": "failed", "running": False, "reason": _GEMFILE, "infra": False,
        "backend": "claude_code", "model": "sonnet", "job_id": "job-2", "pod": None,
        "reason_tail": [_GEMFILE],
    }]
    boot = _boot(
        state="failed", label="Failed on run 1 of 3", attempt=1, reason=_GEMFILE,
        reason_tail=["rbenv: ruby: command not found", _GEMFILE], commit="abc1234", history=history,
        next_attempt_at=_ago(-60),
    )
    _open(page, live_server_url + "/symphonies", store, [_symphony("alpha-sym", boot)])

    cell = _env_cell(page, "alpha-sym")
    expect(cell).to_contain_text(_GEMFILE, timeout=_WAIT_LIVE)
    expect(cell.locator("summary", has_text="History (1)")).to_be_visible(timeout=_WAIT_LIVE)
    cell.locator("summary", has_text="History (1)").click()
    expect(cell.locator("li", has_text="#2")).to_contain_text("Failed after 10m", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_budget_that_is_used_up_says_what_resets_it(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    boot = _boot(
        state="budget_exhausted", label="Budget exhausted after 3 failed runs", attempt=3,
        reason=_GEMFILE, reset_hint="A new commit to the env spec files resets the budget.",
    )
    _open(page, live_server_url + "/symphonies", store, [_symphony("alpha-sym", boot)])

    cell = _env_cell(page, "alpha-sym")
    expect(cell).to_contain_text("Budget exhausted", timeout=_WAIT_LIVE)
    expect(cell).to_contain_text("A new commit to the env spec files resets the budget.", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_status_strip_names_a_bootstrap_that_is_running(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    boot = _boot(state="bootstrapping", label="Bootstrapping, run 2 of 3", attempt=2, started_at=_ago(30))
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle", symphonies=[_symphony(_BOOT_BOARD, boot)]))

    strip = page.locator("#status-bootstrap")
    expect(strip).to_have_text("Env bootstrap: Bootstrapping, run 2 of 3", timeout=_WAIT_LIVE)
    expect(strip).to_be_visible()


@pytest.mark.e2e
def test_the_status_strip_is_empty_when_every_bootstrap_is_ready(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    ready = _boot(state="ready", label="Ready for commit abc1234", commit="abc1234", ready_since=_ago(7200))
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle", symphonies=[_symphony(_BOOT_BOARD, ready)]))

    expect(page.locator("#status-bootstrap")).to_be_hidden(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_card_held_by_the_bootstrap_says_so_on_the_card(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    boot = _boot(state="bootstrapping", label="Bootstrapping, run 2 of 3", attempt=2, started_at=_ago(30))
    symphony = _symphony(_BOOT_BOARD, boot)
    symphony["state"]["board_snapshot"] = {"TODO": ["PVTI_70"], "BLOCKED": [], "IN_PROGRESS": [], "IN_REVIEW": []}
    symphony["state"]["board_titles"] = {"PVTI_70": "Add the held export"}
    symphony["state"]["session_skip_reasons"] = {
        "PVTI_70": {
            "reason": "env_bootstrap",
            "detail": "Waiting for the environment bootstrap (run 2 of 3)",
            "bootstrap_state": "bootstrapping", "attempt": 2, "max_attempts": 3,
        },
    }
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle", symphonies=[symphony]))

    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Add the held export")
    expect(card.locator(".swimlane-wait")).to_have_text(
        "Waiting for the environment bootstrap (run 2 of 3)", timeout=_WAIT_LIVE,
    )


@pytest.mark.e2e
def test_the_health_row_gives_the_reason_a_bootstrap_failed(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    summary = "env cache bootstrap failed for 1 symphony: " + _GEMFILE
    row = {
        "key": "disk_env_cache", "name": "Disk and env cache", "status": "degraded",
        "summary": "62% of disk free; env cache ready for 0 of 1", "reason": summary,
        "fresh_label": "measured", "checked_at": _ago(0), "last_success_at": None,
    }
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(
        phase="idle", phase_label="Idle",
        system_health={"overall": "degraded", "rows": [row]},
    ))

    card = page.locator("#subsystems-section")
    expect(card).to_contain_text(_GEMFILE, timeout=_WAIT_LIVE)
    expect(card).to_contain_text("env cache bootstrap failed for 1 symphony", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_one_symphonys_bootstrap_rows_stay_out_of_anothers_scope(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    """Under alpha's scope the feed shows alpha's bootstrap row and not beta's, failure text included."""
    store.activity_log.record(
        activity_type="env_bootstrap_ready", card_id="", card_title="alpha-sym", stage="env_bootstrap",
        text="Ready for commit abc1234", symphony="alpha-sym",
    )
    store.activity_log.record(
        activity_type="env_bootstrap_failed", card_id="", card_title="beta-sym", stage="env_bootstrap",
        text="Verify failed on run 1 of 3: beta-only verify text", symphony="beta-sym",
    )
    page.goto(live_server_url + "/?symphony=alpha-sym")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(
        phase="idle", phase_label="Idle",
        symphonies=[
            _symphony("alpha-sym", _boot(state="ready", label="Ready for commit abc1234", commit="abc1234")),
            _symphony("beta-sym", _boot(state="failed", label="Failed on run 1 of 3", reason="beta-only verify text")),
        ],
    ))
    feed = page.locator("#activity-feed")
    expect(feed).to_contain_text("Ready for commit abc1234", timeout=_WAIT_LIVE)
    expect(feed).not_to_contain_text("beta-only verify text")


@pytest.mark.e2e
def test_the_health_row_under_a_symphony_names_only_its_own_failure(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    row = {
        "key": "disk_env_cache", "name": "Disk and env cache", "status": "degraded",
        "summary": "62% of disk free; env cache ready for 0 of 2", "fresh_label": "measured",
        "checked_at": _ago(0), "last_success_at": None,
        "reason": "env cache bootstrap failed for 2 symphonies: alpha-sym: alpha-only reason; beta-sym: beta-only reason",
        "env_failures": [
            {"symphony": "alpha-sym", "reason": "alpha-only reason"},
            {"symphony": "beta-sym", "reason": "beta-only reason"},
        ],
    }
    page.goto(live_server_url + "/?symphony=alpha-sym")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(
        phase="idle", phase_label="Idle",
        symphonies=[_symphony("alpha-sym", _boot()), _symphony("beta-sym", _boot())],
        system_health={"overall": "degraded", "rows": [row]},
    ))
    card = page.locator("#subsystems-section")
    expect(card).to_contain_text("alpha-only reason", timeout=_WAIT_LIVE)
    expect(card).to_contain_text("1 other symphony has a failing bootstrap", timeout=_WAIT_LIVE)
    expect(card).not_to_contain_text("beta-only reason")

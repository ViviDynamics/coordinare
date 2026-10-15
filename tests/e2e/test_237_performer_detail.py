"""237 and 239: the performer detail page's Live Events and metrics, in a real browser.

The tool rows show the command, narration is an Agent row, a failed result is in the error state,
each row links to its transcript record, the role name is the one heading, no line shows a
placeholder, and the shim's health probes are kept out of the process log.

Runs in Chromium and Firefox (--browser firefox). The renderers are checked under node in
tests/js/performer_detail_237_checks.js.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_CARD = "PVTI_237"
_RUN = "a237run"
_ROLES = [{"role": "implementing", "active": 1, "max": 2, "queued": 0}]
_DETAIL = "#performers-page-detail"
_PROBE = "2026-10-14 09:00:01 [info] claude_code shim request method=HEAD path=/api/hello status=404 latency_ms=0"
_MODEL = "2026-10-14 09:00:02 [info] claude_code shim request method=POST path=/v1/messages status=200 latency_ms=812"

_EVENTS = [
    {"type": "progress", "text": "Reading the failing test first.", "detail": "", "timestamp": "2026-10-14T09:00:03+00:00",
     "transcript_seq": 6},
    {"type": "tool_use", "text": "Bash", "detail": "tu_1", "tool": "Bash", "arg": "uv run pytest -q",
     "timestamp": "2026-10-14T09:00:04+00:00", "transcript_seq": 7},
    {"type": "tool_use", "text": "result: 1 failed", "detail": "tu_1", "status": "error",
     "timestamp": "2026-10-14T09:00:05+00:00", "transcript_seq": 8},
]


def _broadcast(store: DashboardStore) -> None:
    session = _active_session(
        _CARD, "Add login", "implementing", issue_number=237,
        session_id=_RUN, performer_backend="claude_code",
        performer_events=_EVENTS,
        performer_logs=[_PROBE, _PROBE, _MODEL],
        performer_metrics={"pid": 1, "memory_bytes": 104857600, "cpu_percent": 12.5, "child_pids": []},
    )
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer",
        phase_label="Watching an agent",
        active_sessions=[session],
        role_utilization=_ROLES,
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    ))


def _open_run(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url + "/performers/implementing")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    expect(page.locator(_DETAIL)).to_contain_text("Live Events", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_tool_row_shows_the_command_it_ran(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_run(page, live_server_url, store)
    tool = page.locator(f"{_DETAIL} .af-tool")
    expect(tool).to_have_text("Bash · uv run pytest -q", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_narration_is_an_agent_row_and_not_a_tool_call(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_run(page, live_server_url, store)
    narration = page.locator(f"{_DETAIL} .af-row-agent .af-narration")
    expect(narration).to_have_text("Reading the failing test first.", timeout=_WAIT_LIVE)
    expect(page.locator(f"{_DETAIL} .af-row-agent .af-kind")).to_have_text("AGENT")
    # The tool call is its own row with its own look, and the narration is not inside it.
    expect(page.locator(f"{_DETAIL} .af-tool")).to_have_count(1)
    expect(page.locator(f"{_DETAIL} .af-tool .af-narration")).to_have_count(0)


@pytest.mark.e2e
def test_a_failed_tool_result_is_in_the_error_state(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_run(page, live_server_url, store)
    failed = page.locator(f"{_DETAIL} .af-result-error")
    expect(failed).to_have_text("result: 1 failed", timeout=_WAIT_LIVE)
    expect(page.locator(f"{_DETAIL} .af-row-tools .af-kind").filter(has_text="RESULT")).to_have_count(1)


@pytest.mark.e2e
def test_each_event_links_to_its_transcript_record(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_run(page, live_server_url, store)
    link = page.locator(f"{_DETAIL} a.run-entry-link[href='/runs/{_RUN}#e7']")
    expect(link).to_have_count(1, timeout=_WAIT_LIVE)
    expect(page.locator(f"{_DETAIL} a.run-entry-link[href='/runs/{_RUN}#e8']")).to_have_count(1)


@pytest.mark.e2e
def test_the_role_name_is_the_only_heading_on_the_run(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_run(page, live_server_url, store)
    expect(page.locator("#performers-page-heading")).to_be_hidden()
    # One visible heading, and it is the run's role, not the page title.
    headings = page.locator("#performers-page h2:visible")
    expect(headings).to_have_count(1, timeout=_WAIT_LIVE)
    expect(headings).not_to_have_text("Performers")


@pytest.mark.e2e
def test_no_line_of_the_detail_shows_a_placeholder(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_run(page, live_server_url, store)
    expect(page.locator(f"{_DETAIL} .af-tool")).to_be_visible(timeout=_WAIT_LIVE)
    text = page.locator(_DETAIL).inner_text()
    for placeholder in ("No run stats available yet", "No live metrics", "undefined", "null", "NaN", "unknown", "PID 1 ·"):
        assert placeholder not in text, f"placeholder on the detail page: {placeholder!r}"
    lines = [line.strip() for line in text.splitlines()]
    assert "performer" not in lines, "the bare backend fallback is on the page"
    assert "-" not in lines, "a bare dash is on the page"


@pytest.mark.e2e
def test_health_probes_are_filtered_out_of_the_process_log(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open_run(page, live_server_url, store)
    log = page.locator(f"{_DETAIL} .detail-log")
    expect(log).to_contain_text("path=/v1/messages", timeout=_WAIT_LIVE)
    expect(log).not_to_contain_text("/api/hello")
    expect(log).not_to_contain_text("method=HEAD")
    expect(page.locator(f"{_DETAIL} .detail-log > div")).to_have_count(1)

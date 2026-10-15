"""#150: the History page is a table of card runs, read from the server, in a real browser.

Each test serves a transcript root written into a temporary directory: two symphonies the
daemon runs, one folder for a symphony it does not run, and the conductor's checks. The API
and the server-side listing are in tests/unit/test_150_run_history.py, and the client helpers
in tests/unit/test_150_history_js.py. Runs in Chromium and Firefox (--browser firefox).
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import socket
import threading
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.services.run_transcript import TranscriptCollector, transcript_path
from tests.e2e.conftest import _free_port, _mock_daemon, _mock_health, _mock_metrics
from tests.e2e.test_139_symphony_switcher import _connected, _snapshot, _two_symphonies
from tests.e2e.test_dashboard_browser import _WAIT_NAV

ALPHA = "alpha-sym"
BETA = "beta-sym"
HIDDEN = "hidden-sym"
CONDUCTOR = "_conductor"
# Yesterday at 10:00 UTC, not a fixed day: the dashboard's token ledger prunes runs older than its
# window from the real clock, and a fixed day would be pruned from the snapshot's totals. Not today
# either: between 00:00 and 10:00 UTC today's 10:00 is in the future, and the paging anchor (the
# server's clock on page one) rightly hides runs written after it, which emptied page two.
BASE = (datetime.now(UTC) - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
TODAY = BASE.date().isoformat()
TOMORROW = (BASE + timedelta(days=1)).date().isoformat()
PAGE_SIZE = 50
PAGED_RUNS = 55
MODEL = "claude-test-model"
PR_URL = "https://github.com/vividynamics/coordinare/pull/150"
WAIT_PAGE = 10_000
SERVER_START_TIMEOUT_SECONDS = 5.0


def _at(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


def _header(run: str, card: str, title: str, stage: str, minutes: float = 0,
            conductor: dict[str, str] | None = None) -> dict[str, Any]:
    header: dict[str, Any] = {
        "type": "run_header", "seq": 0, "header_version": 1, "ts": _at(minutes), "run_id": run,
        "persona": {"name": stage, "version": 1, "version_sha256": "ab" * 32},
        "performer": {"id": "perf-1", "backend": "claude-code", "transport": "http", "endpoint": "ep-1"},
        "model": {"model": MODEL},
        "card": {"id": card, "issue_number": 150, "title": title},
    }
    if conductor is not None:
        header["conductor"] = conductor
    return header


def _records(run: str, minutes: float, *, failed: bool = False, pr: bool = False, usage: bool = True) -> list[dict[str, Any]]:
    # The ledger keys each usage record by the run id the record carries, as the writer stamps it.
    records: list[dict[str, Any]] = [{"ts": _at(minutes + 0.1), "run_id": run, "seq": 1, "type": "user", "text": "work"}]
    if usage:
        records.append({"ts": _at(minutes + 0.2), "run_id": run, "seq": 2, "type": "usage", "scope": "message",
                        "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read": 0, "cache_write": 0,
                                  "model": MODEL}})
    text = f"Opened {PR_URL}" if pr else "Stopped."
    records.append({"ts": _at(minutes + 1), "run_id": run, "seq": 3, "type": "result", "is_error": failed, "text": text})
    return records


def _write(root: Path, symphony: str, card: str, run: str, header: dict[str, Any],
           records: list[dict[str, Any]], mtime: float) -> None:
    if symphony == CONDUCTOR:
        path = root / CONDUCTOR / card / f"{run}.jsonl"
    else:
        path = transcript_path(root, symphony=symphony, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(header)] + [json.dumps(record) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _seed(root: Path) -> None:
    """Every run the browser tests look for. mtimes ascend with the minutes, so the newest is last."""
    # The files are written on the day of the records, so the ledger's window holds them.
    mtime = BASE.timestamp()
    _write(root, ALPHA, "PVTI_A1", "run-a1", _header("run-a1", "PVTI_A1", "Alpha card", "implementing"),
           _records("run-a1", 0, pr=True), mtime + 10)
    _write(root, ALPHA, "PVTI_A1", "run-a2", _header("run-a2", "PVTI_A1", "Alpha card", "qa", 60),
           _records("run-a2", 60, failed=True), mtime + 20)
    _write(root, BETA, "PVTI_B1", "run-b1", _header("run-b1", "PVTI_B1", "Beta card", "implementing", 120),
           _records("run-b1", 120), mtime + 30)
    _write(root, HIDDEN, "PVTI_H1", "run-h1", _header("run-h1", "PVTI_H1", "Hidden card", "implementing", 180),
           _records("run-h1", 180), mtime + 40)
    for index in range(PAGED_RUNS):
        run = f"run-p{index:03d}"
        _write(root, ALPHA, "PVTI_P1", run, _header(run, "PVTI_P1", "Paged card", "implementing", 200 + index),
               _records(run, 200 + index), mtime + 100 + index)
    for index, decision in enumerate(["proceed", "proceed", "proceed", "wait"]):
        run = f"run-c{index}"
        conductor = {"kind": "triage", "decision": decision, "symphony": ALPHA}
        _write(root, CONDUCTOR, "PVTI_A1", run,
               _header(run, "PVTI_A1", "Alpha card", "conductor", 300 + index, conductor=conductor),
               _records(run, 300 + index), mtime + 200 + index)


@pytest.fixture(scope="module")
def transcript_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("history-transcripts")
    _seed(root)
    return root


@pytest.fixture(scope="module")
def store() -> DashboardStore:
    return DashboardStore()


@pytest.fixture(scope="module")
def history_server_url(store: DashboardStore, transcript_root: Path) -> Generator[str, None, None]:
    """A live server whose daemon runs two symphonies and names the transcript root."""
    daemon = _mock_daemon()
    collector = TranscriptCollector(transcript_root, enabled=True, max_bytes_per_run=10**9, retention_days=0)
    # The daemon rebuilds the token ledger from the files at boot; the browser fixture does the same.
    collector.usage.load_from_transcripts(transcript_root, now=BASE + timedelta(days=1))
    daemon.state["transcript_collector"] = collector
    daemon.state["symphony_configs"] = {ALPHA: object(), BETA: object()}
    port = _free_port()
    app = create_dashboard_app(store, daemon, _mock_metrics(), _mock_health())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    server_loops: queue.SimpleQueue[asyncio.AbstractEventLoop] = queue.SimpleQueue()

    async def serve() -> None:
        server_loops.put(asyncio.get_running_loop())
        await server.serve()

    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("Live server did not start within 5 seconds")
    server_loop = server_loops.get(timeout=SERVER_START_TIMEOUT_SECONDS)
    # asyncio.Queue must be written on its owning loop, as in live_server_url.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            store.broadcaster, "broadcast",
            partial(server_loop.call_soon_threadsafe, store.broadcaster.broadcast),
        )
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=SERVER_START_TIMEOUT_SECONDS)


def _history(page: Page, base: str, store: DashboardStore, query: str = "") -> None:
    """Open the History page, wait for the stream, and send the snapshot that carries the symphonies."""
    _connected(page, base + "/history" + query)
    store.broadcaster.broadcast(_snapshot(_two_symphonies()))
    expect(page.locator("#history-page-section form.history-filters")).to_be_visible(timeout=WAIT_PAGE)


def _links(page: Page) -> list[str]:
    return page.eval_on_selector_all(
        "#history-page-section table a[data-history-link]",
        "els => els.map(e => e.getAttribute('data-history-link'))",
    )


def _apply(page: Page, **fields: str) -> None:
    for name, value in fields.items():
        control = page.locator(f"#history-page-section [name='{name}']")
        if control.evaluate("el => el.tagName") == "SELECT":
            control.select_option(value)
        else:
            control.fill(value)
    page.locator("#history-page-section button[type='submit']").click()


# --- the table and its links ---------------------------------------------------- #


@pytest.mark.e2e
def test_the_history_table_lists_runs_with_a_transcript_link(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    _apply(page, card="PVTI_A1")
    expect(page.locator("#history-page-section table a[data-history-link]").first).to_be_visible(timeout=WAIT_PAGE)
    link = page.locator("#history-page-section a[data-history-link='/runs/alpha-sym/PVTI_A1/run-a1']")
    expect(link).to_have_attribute("href", "/runs/alpha-sym/PVTI_A1/run-a1")
    row = link.locator("xpath=ancestor::tr")
    expect(row).to_contain_text("Alpha card")
    # 233: the card cell opens the card page, not History filtered to the card.
    expect(row.locator("a[href='/cards/alpha-sym/PVTI_A1']")).to_have_count(1)
    expect(row).to_contain_text("1,200")
    expect(row).to_contain_text("Completed")
    expect(row.locator("a[href='" + PR_URL + "']")).to_have_count(1)


@pytest.mark.e2e
def test_a_transcript_link_opens_that_run(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    _apply(page, card="PVTI_A1")
    page.locator("#history-page-section a[data-history-link='/runs/alpha-sym/PVTI_A1/run-a1']").click()
    expect(page).to_have_url(f"{history_server_url}/runs/alpha-sym/PVTI_A1/run-a1", timeout=_WAIT_NAV)
    expect(page.locator("#runs-page")).to_be_visible(timeout=WAIT_PAGE)


@pytest.mark.e2e
def test_an_unknown_cost_says_not_recorded_and_why(page: Page, history_server_url: str, store: DashboardStore) -> None:
    """No price table is configured here, so the tokens are known and the cost is not."""
    _history(page, history_server_url, store)
    _apply(page, card="PVTI_A1")
    row = page.locator("#history-page-section tr", has=page.locator("a[data-history-link='/runs/alpha-sym/PVTI_A1/run-a2']"))
    expect(row).to_contain_text("1,200")
    expect(row.locator(".history-missing").first).to_have_text("not recorded")
    expect(row.locator(".history-missing").first).to_have_attribute(
        "title", f"no price for {MODEL} (no price configured)",
    )


# --- the filters ----------------------------------------------------------------- #


@pytest.mark.e2e
def test_the_symphony_filter_keeps_one_symphony(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    _apply(page, symphony=BETA)
    expect(page.locator("#history-page-section a[data-history-link]").first).to_have_attribute(
        "data-history-link", "/runs/beta-sym/PVTI_B1/run-b1", timeout=WAIT_PAGE,
    )
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(1)


@pytest.mark.e2e
def test_the_card_filter_keeps_one_card(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    _apply(page, card="PVTI_A1")
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(2, timeout=WAIT_PAGE)


@pytest.mark.e2e
def test_the_stage_filter_keeps_one_stage(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    _apply(page, stage="qa")
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(1, timeout=WAIT_PAGE)
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_attribute(
        "data-history-link", "/runs/alpha-sym/PVTI_A1/run-a2", timeout=WAIT_PAGE,
    )
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(1)


@pytest.mark.e2e
def test_the_outcome_filter_keeps_one_outcome(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    _apply(page, outcome="failed")
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(1, timeout=WAIT_PAGE)
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_attribute(
        "data-history-link", "/runs/alpha-sym/PVTI_A1/run-a2", timeout=WAIT_PAGE,
    )
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(1)


@pytest.mark.e2e
def test_the_date_range_keeps_the_days_asked_for(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    _apply(page, since=TOMORROW)
    expect(page.locator("#history-page-section")).to_contain_text("No runs match these filters.", timeout=WAIT_PAGE)
    _apply(page, since=TODAY, until=TODAY)
    expect(page.locator("#history-page-section a[data-history-link]").first).to_be_visible(timeout=WAIT_PAGE)


@pytest.mark.e2e
def test_clear_puts_the_filters_back(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    _apply(page, outcome="failed")
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(1, timeout=WAIT_PAGE)
    page.locator("#history-page-section [data-history-action='reset']").click()
    expect(page.locator("#history-page-section a[data-history-link]").first).to_be_visible(timeout=WAIT_PAGE)
    expect(page.locator("#history-page-section [name='outcome']")).to_have_value("")


# --- pagination ------------------------------------------------------------------ #


@pytest.mark.e2e
def test_older_and_newer_page_through_the_server_listing(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    expect(page.locator("#history-page-section [data-history-action='newer']")).to_be_disabled(timeout=WAIT_PAGE)
    expect(page.locator("#history-page-section [data-history-action='older']")).to_be_enabled(timeout=WAIT_PAGE)
    first_page = _links(page)
    assert 0 < len(first_page) <= PAGE_SIZE
    page.locator("#history-page-section [data-history-action='older']").click()
    expect(page.locator("#history-page-section [data-history-action='newer']")).to_be_enabled(timeout=WAIT_PAGE)
    second_page = _links(page)
    assert second_page, "the second page should hold the runs the first did not"
    assert not set(first_page) & set(second_page)
    page.locator("#history-page-section [data-history-action='newer']").click()
    # Newer is disabled while the page loads, so wait for the first page's rows, not for the button.
    expect(page.locator("#history-page-section a[data-history-link]").first).to_have_attribute(
        "data-history-link", first_page[0], timeout=WAIT_PAGE,
    )
    expect(page.locator("#history-page-section [data-history-action='newer']")).to_be_disabled(timeout=WAIT_PAGE)
    assert _links(page) == first_page


@pytest.mark.e2e
def test_every_run_is_reachable_across_the_pages(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    seen: set[str] = set()
    for _ in range(4):
        expect(page.locator("#history-page-section .history-pager")).to_be_visible(timeout=WAIT_PAGE)
        seen.update(_links(page))
        older = page.locator("#history-page-section [data-history-action='older']")
        if older.is_disabled():
            break
        older.click()
        # The next page has painted once Newer can go back to the page just left.
        expect(page.locator("#history-page-section [data-history-action='newer']")).to_be_enabled(timeout=WAIT_PAGE)
    paged = {f"/runs/alpha-sym/PVTI_P1/run-p{index:03d}" for index in range(PAGED_RUNS)}
    assert paged <= seen
    assert "/runs/beta-sym/PVTI_B1/run-b1" in seen


# --- checks ---------------------------------------------------------------------- #


@pytest.mark.e2e
def test_identical_checks_collapse_into_one_line(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    checks = page.locator("#history-page-section details.history-checks")
    expect(checks).to_be_visible(timeout=WAIT_PAGE)
    expect(checks.locator("summary")).to_have_text("Checks (4)")
    expect(checks).not_to_have_attribute("open", "")
    checks.locator("summary").click()
    expect(checks).to_have_attribute("open", "")
    expect(checks.locator("tbody tr")).to_have_count(2)
    expect(checks).to_contain_text("3 checks, all completed")
    expect(checks).to_contain_text("Completed")


# --- scope ----------------------------------------------------------------------- #


@pytest.mark.e2e
def test_a_symphony_the_viewer_cannot_see_is_not_listed(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    expect(page.locator("#history-page-section a[data-history-link]").first).to_be_visible(timeout=WAIT_PAGE)
    expect(page.locator("#history-page-section")).not_to_contain_text("Hidden card")
    assert not [link for link in _links(page) if "hidden-sym" in link]


@pytest.mark.e2e
def test_the_server_refuses_a_symphony_the_daemon_does_not_run(page: Page, history_server_url: str) -> None:
    response = page.request.get(f"{history_server_url}/api/history/runs?symphony={HIDDEN}")
    assert response.status == 404
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.e2e
def test_the_header_scope_limits_the_history_to_its_symphony(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store, query=f"?symphony={BETA}")
    expect(page.locator("#history-page-section select[name='symphony']")).to_have_value(BETA, timeout=WAIT_PAGE)
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(1, timeout=WAIT_PAGE)
    expect(page.locator("#history-page-section")).not_to_contain_text("Alpha card")


# --- review round: the filters live in the URL, and a run written while paging is announced --- #


@pytest.mark.e2e
def test_the_filters_survive_a_reload_because_they_are_in_the_url(page: Page, history_server_url: str, store: DashboardStore) -> None:
    _history(page, history_server_url, store)
    _apply(page, outcome="failed", card="PVTI_A1")
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(1, timeout=WAIT_PAGE)
    expect(page).to_have_url(re.compile(r"h\.outcome=failed"), timeout=WAIT_PAGE)
    page.reload(wait_until="domcontentloaded", timeout=WAIT_PAGE)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=WAIT_PAGE)
    store.broadcaster.broadcast(_snapshot(_two_symphonies()))
    expect(page.locator("#history-page-section select[name='outcome']")).to_have_value("failed", timeout=WAIT_PAGE)
    expect(page.locator("#history-page-section a[data-history-link]")).to_have_count(1, timeout=WAIT_PAGE)


@pytest.mark.e2e
def test_a_run_written_while_paging_is_announced_not_silently_skipped(
    page: Page, history_server_url: str, store: DashboardStore, transcript_root: Path,
) -> None:
    _history(page, history_server_url, store)
    expect(page.locator("#history-page-section [data-history-action='older']")).to_be_enabled(timeout=WAIT_PAGE)
    newest = transcript_path(transcript_root, symphony=ALPHA, card_id="PVTI_LATE", run_id="run-late")
    newest.parent.mkdir(parents=True, exist_ok=True)
    header = _header("run-late", "PVTI_LATE", "Late card", "implementing", 0)
    newest.write_text(json.dumps(header) + "\n" + json.dumps(_records("run-late", 0)[-1]) + "\n", encoding="utf-8")
    # Written now, so its modification time is after page one's wall-clock anchor.
    written = time.time_ns()
    os.utime(newest, ns=(written, written))
    try:
        page.locator("#history-page-section [data-history-action='older']").click()
        expect(page.locator("#history-page-section")).to_contain_text(
            "New runs since this list loaded: refresh", timeout=WAIT_PAGE,
        )
        expect(page.locator("#history-page-section")).not_to_contain_text("Late card")
    finally:
        newest.unlink()
        newest.parent.rmdir()

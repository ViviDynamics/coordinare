"""Logs page browser contracts for Chromium and Firefox."""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from itertools import count
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.services.event_store import EventStore
from tests.e2e.conftest import (
    _free_port,
    _mock_daemon,
    _mock_health,
    _mock_metrics,
    mark_daemon_started,
)
from tests.e2e.test_139_symphony_switcher import _snapshot, _two_symphonies

WAIT_MS = 10_000
WAIT_SECONDS = 10
START_INTERVAL = 0.05
PAGE_ROWS = 50
SEED_ROWS = 55
BETA_TRANSCRIPT_SEQ = SEED_ROWS
ALPHA = "alpha-sym"
BETA = "beta-sym"


@dataclass
class LogsServer:
    url: str
    events: EventStore
    loop: asyncio.AbstractEventLoop
    sequence: Iterator[int]

    def emit(self, message: str, symphony: str = ALPHA) -> None:
        asyncio.run_coroutine_threadsafe(_emit(self.events, message, symphony, self.sequence), self.loop).result(WAIT_SECONDS)


async def _emit(events: EventStore, message: str, symphony: str, sequence: Iterator[int]) -> None:
    events.emit({"symphony": symphony, "card_id": "card", "run_id": "run", "stage": "qa",
                 "source": "performer", "kind": "tool", "level": "info", "message": message,
                 "data": {"transcript_seq": next(sequence), "detail": "<literal>"}})
    await events.flush()


@pytest.fixture
def logs_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[LogsServer, None, None]:
    store = DashboardStore()
    daemon = _mock_daemon()
    events = EventStore(tmp_path / "events.db")
    daemon.state["event_store"] = events
    daemon.state["symphony_configs"] = {ALPHA: object(), BETA: object()}
    monkeypatch.setattr(store, "build_snapshot", lambda *args: _snapshot(_two_symphonies()))
    mark_daemon_started(daemon)
    app = create_dashboard_app(store, daemon, _mock_metrics(), _mock_health())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    loop = asyncio.new_event_loop()
    sequence = count()

    async def serve() -> None:
        await events.start()
        try:
            for index in range(SEED_ROWS):
                await _emit(events, f"alpha row {index}", ALPHA, sequence)
            await _emit(events, "beta marker", BETA, sequence)
            await server.serve()
        finally:
            await events.close()

    thread = threading.Thread(target=lambda: loop.run_until_complete(serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + WAIT_SECONDS
    while not server.started and time.monotonic() < deadline:
        time.sleep(START_INTERVAL)
    assert server.started, "Logs test server did not start"
    try:
        yield LogsServer(f"http://127.0.0.1:{port}", events, loop, sequence)
    finally:
        loop.call_soon_threadsafe(store.shutdown)
        server.should_exit = True
        thread.join(timeout=WAIT_SECONDS)
        assert not thread.is_alive()
        loop.close()


def _open(page: Page, server: LogsServer, query: str = "") -> None:
    page.goto(server.url + "/logs" + query)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=WAIT_MS)
    expect(page.locator("#logs-rows tr")).to_have_count(PAGE_ROWS, timeout=WAIT_MS)


@pytest.mark.e2e
def test_filters_and_header_scope(page: Page, logs_server: LogsServer) -> None:
    _open(page, logs_server)
    fields: dict[str, str] = {
        "symphony": BETA, "card_id": "card", "run_id": "run", "stage": "qa",
        "source": "performer", "kind": "tool", "level": "info", "text": "beta marker",
        "start": "2020-01-01T00:00", "end": "2099-01-01T00:00",
    }
    for key, value in fields.items():
        page.locator(f"#logs-page [name='{key}']").fill(value)
    page.locator("#logs-page button[type='submit']").click()
    expect(page.locator("#logs-rows tr")).to_have_count(1, timeout=WAIT_MS)
    expect(page.locator("#logs-rows")).to_contain_text("beta marker")
    page.goto(logs_server.url + "/logs?symphony=" + ALPHA)
    expect(page.locator("#logs-rows tr")).to_have_count(PAGE_ROWS, timeout=WAIT_MS)
    expect(page.locator("#logs-rows")).not_to_contain_text("beta marker")
    expect(page.locator("#logs-page [name='symphony']")).to_be_disabled()


@pytest.mark.e2e
def test_cursor_paging(page: Page, logs_server: LogsServer) -> None:
    _open(page, logs_server)
    first = page.locator("#logs-rows tr").evaluate_all("rows => rows.map(row => row.dataset.eventId)")
    page.locator("[data-logs-action='older']").click()
    expect(page.locator("#logs-rows tr")).to_have_count(SEED_ROWS + 1 - PAGE_ROWS, timeout=WAIT_MS)
    second = page.locator("#logs-rows tr").evaluate_all("rows => rows.map(row => row.dataset.eventId)")
    assert not set(first) & set(second)
    expect(page.locator("#logs-live")).not_to_be_checked()
    page.locator("[data-logs-action='newer']").click()
    expect(page.locator("#logs-rows tr")).to_have_count(PAGE_ROWS, timeout=WAIT_MS)
    assert page.locator("#logs-rows tr").evaluate_all("rows => rows.map(row => row.dataset.eventId)") == first


@pytest.mark.e2e
def test_live_rows_and_paused_view(page: Page, logs_server: LogsServer) -> None:
    _open(page, logs_server)
    page.locator("#logs-live").check()
    logs_server.emit("live marker")
    expect(page.locator("#logs-rows")).to_contain_text("live marker", timeout=WAIT_MS)
    page.locator("#logs-live").uncheck()
    logs_server.emit("paused marker")
    # Wait for a later SSE event to be handled before asserting the paused view.
    page.evaluate("window.logsNotificationSeen = false; es.addEventListener('events_changed', function() { window.logsNotificationSeen = true; }, {once:true});")
    logs_server.emit("later marker")
    page.wait_for_function("window.logsNotificationSeen", timeout=WAIT_MS)
    expect(page.locator("#logs-rows")).not_to_contain_text("paused marker")
    page.locator("#logs-live").check()
    expect(page.locator("#logs-rows")).to_contain_text("paused marker", timeout=WAIT_MS)


@pytest.mark.e2e
def test_row_links_and_expand_view(page: Page, logs_server: LogsServer) -> None:
    _open(page, logs_server)
    row = page.locator("#logs-rows tr").first
    record_path = f"/runs/beta-sym/card/run#e{BETA_TRANSCRIPT_SEQ}"
    expect(row.locator("a[href='/cards/beta-sym/card']")).to_have_count(1)
    expect(row.locator("a[href='/runs/beta-sym/card/run']")).to_have_count(1)
    expect(row.locator(f"a[href='{record_path}']")).to_have_count(1)
    row.locator("summary").click()
    expect(row.locator("pre")).to_be_visible()
    expect(row.locator("pre")).to_contain_text(f'"transcript_seq": {BETA_TRANSCRIPT_SEQ}')
    expect(row.locator("pre")).to_contain_text("<literal>")
    expect(row.locator("pre literal")).to_have_count(0)
    row.locator(f"a[href='{record_path}']").click()
    expect(page).to_have_url(logs_server.url + record_path, timeout=WAIT_MS)
    expect(page.locator("#runs-page")).to_be_visible()

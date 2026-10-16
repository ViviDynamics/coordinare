"""#233: the card page at /cards/<symphony>/<card>, in a real browser.

The page needs no live session: it reads the timeline endpoint only. It is reached from the History
row's card cell, the Done lane, and the live card detail's "Full timeline" link. An unknown card
says no timeline is recorded, a hostile address says it is not a card address, and a viewer scoped
to another symphony sees a scoped note instead of the card. Back and forward work, because the
page has its own address. The server side is in tests/unit/test_233_card_page.py and the client
helpers in tests/unit/test_233_card_page_js.py. Runs in Chromium and Firefox (--browser firefox).
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.services.card_lifecycle import CardLifecycleLog
from coordinare.services.run_transcript import TranscriptCollector, transcript_path
from tests.e2e.conftest import (
    _free_port,
    _mock_daemon,
    _mock_health,
    _mock_metrics,
    mark_daemon_started,
)
from tests.e2e.test_139_symphony_switcher import _connected
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _active_session, _full_snapshot

ALPHA = "alpha-sym"
BETA = "beta-sym"
CARD = "PVTI_A1"
DONE_CARD = "PVTI_D1"
BETA_CARD = "PVTI_B1"
UNKNOWN_CARD = "PVTI_NOPE"
RUN = "run233a1"
REVIEW_RUN = "run233a2"
DONE_RUN = "run233d1"
BETA_RUN = "run233b1"
T0 = datetime(2026, 10, 14, 9, 0, 0, tzinfo=UTC)
WAIT_PAGE = 10_000
HOSTILE = "%3Cimg%20src%3Dx%20onerror%3Dwindow.__pwned%3D1%3E"
NOTE_START = "This card belongs to " + BETA + "."


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _header(run: str, card: str, persona: str) -> dict[str, Any]:
    return {
        "type": "run_header", "seq": 0, "header_version": 1, "ts": _at(0), "run_id": run,
        "persona": {"name": persona, "version": 2, "version_sha256": "ab" * 32,
                    "changed_at": _at(-3600), "changed_by": "local", "source": "dashboard"},
        "performer": {"id": "perf-1", "backend": "claude-code", "transport": "http", "endpoint": "ep-1"},
        "model": {"model": "claude-test-model"},
        "card": {"id": card, "issue_number": 233, "title": "Card page"},
    }


def _write_run(root: Path, address: tuple[str, str], run: str, persona: str, length: float) -> None:
    symphony, card = address
    path = transcript_path(root, symphony=symphony, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        _header(run, card, persona),
        {"ts": _at(1), "seq": 1, "type": "user", "text": "Work on it."},
        {"ts": _at(length), "seq": 2, "type": "result", "is_error": False, "text": "Done."},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _log() -> CardLifecycleLog:
    log = CardLifecycleLog()
    log.record(ALPHA, CARD, "dispatched", stage="implementing", at=_at(0))
    log.record(ALPHA, DONE_CARD, "dispatched", stage="implementing", at=_at(0))
    log.record(ALPHA, DONE_CARD, "merged", stage="closing_review", at=_at(200))
    log.record(BETA, BETA_CARD, "dispatched", stage="reviewing", at=_at(0))
    return log


def _symphony(name: str, number: int, repo: str, *, columns: dict[str, list[str]],
              titles: dict[str, str], sessions: list[str]) -> dict[str, Any]:
    board = {"TODO": [], "BLOCKED": [], "IN_PROGRESS": [], "IN_REVIEW": [], "BACKLOG": [], "DONE": []}
    board.update(columns)
    return {
        "name": name, "priority": 0, "github_project_number": number, "github_org": "acme", "repo": repo,
        "state": {
            "board_snapshot": board, "board_titles": titles,
            "board_issue_numbers": {}, "board_issue_urls": {}, "board_pr_urls": {},
            "todo_waits": {}, "todo_capacity": None, "session_skip_reasons": {},
            "active_sessions": {sid: {} for sid in sessions},
        },
    }


def _symphonies() -> list[dict[str, Any]]:
    return [
        _symphony(ALPHA, 16, "alpha-repo", columns={"IN_PROGRESS": [CARD], "DONE": [DONE_CARD]},
                  titles={CARD: "Alpha card", DONE_CARD: "Shipped card"}, sessions=[CARD]),
        _symphony(BETA, 2, "beta-repo", columns={"IN_PROGRESS": [BETA_CARD]},
                  titles={BETA_CARD: "Beta card"}, sessions=[BETA_CARD]),
    ]


def _merged_now() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()


def _snapshot(*, active: bool = True, done: bool = True) -> dict[str, Any]:
    pipeline: dict[str, Any] = {}
    if done:
        pipeline[ALPHA] = {DONE_CARD: {"current": None, "statuses": [], "checks": None, "merged_at": _merged_now()}}
    sessions = [_active_session(CARD, "Alpha card", "implementing", phase="monitoring_performer")] if active else []
    return _full_snapshot(symphonies=_symphonies(), active_sessions=sessions, card_pipeline=pipeline)


@pytest.fixture(scope="module")
def transcript_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("card-page-transcripts")
    _write_run(root, (ALPHA, CARD), RUN, "implementer", 150.0)
    _write_run(root, (ALPHA, CARD), REVIEW_RUN, "reviewer", 30.0)
    _write_run(root, (ALPHA, DONE_CARD), DONE_RUN, "implementer", 90.0)
    _write_run(root, (BETA, BETA_CARD), BETA_RUN, "reviewer", 40.0)
    return root


@pytest.fixture(scope="module")
def card_daemon(transcript_root: Path) -> Any:
    daemon = _mock_daemon("monitoring_performer")
    daemon.state.update({
        "symphony_configs": {ALPHA: SimpleNamespace(), BETA: SimpleNamespace()},
        "symphony_states": {},
        "lifecycle_sequence": ["assessing", "implementing", "reviewing", "closing_review"],
        "active_sessions": {},
        "card_lifecycle": _log(),
        "transcript_collector": TranscriptCollector(transcript_root, enabled=True, max_bytes_per_run=10**9,
                                                    retention_days=0),
    })
    return daemon


@pytest.fixture(scope="module")
def store() -> DashboardStore:
    return DashboardStore()


@pytest.fixture(scope="module")
def card_url(store: DashboardStore, card_daemon: Any) -> Generator[str, None, None]:
    port = _free_port()
    mark_daemon_started(card_daemon)
    app = create_dashboard_app(store, card_daemon, _mock_metrics(), _mock_health())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
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
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _history_row_link(page: Page, store: DashboardStore, base: str, card: str) -> Any:
    _connected(page, base + "/history")
    store.broadcaster.broadcast(_snapshot())
    expect(page.locator("#history-page-section form.history-filters")).to_be_visible(timeout=WAIT_PAGE)
    link = page.locator(f"#history-page-section a[data-history-card='/cards/{ALPHA}/{card}']").first
    expect(link).to_be_visible(timeout=WAIT_PAGE)
    return link


# --- the links that open the page ----------------------------------------------- #


@pytest.mark.e2e
def test_a_history_row_opens_the_card_page(page: Page, card_url: str, store: DashboardStore) -> None:
    link = _history_row_link(page, store, card_url, CARD)
    expect(link).to_have_attribute("href", f"/cards/{ALPHA}/{CARD}")
    link.click()
    expect(page).to_have_url(f"{card_url}/cards/{ALPHA}/{CARD}", timeout=WAIT_PAGE)
    expect(page.locator("#card-page")).to_be_visible(timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section .tl-tracker")).to_be_attached(timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section")).to_contain_text(CARD)


@pytest.mark.e2e
def test_a_done_lane_card_opens_the_card_page(page: Page, card_url: str, store: DashboardStore) -> None:
    _connected(page, card_url + "/")
    store.broadcaster.broadcast(_snapshot())
    lane = page.locator("#done-lane")
    expect(lane.locator("summary .swimlane-col-count")).to_have_text("1", timeout=_WAIT_LIVE)
    lane.locator("summary").click()
    link = lane.locator(".done-lane-list a")
    expect(link).to_have_attribute("href", f"/cards/{ALPHA}/{DONE_CARD}")
    link.click()
    expect(page).to_have_url(f"{card_url}/cards/{ALPHA}/{DONE_CARD}", timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section .tl-tracker")).to_be_attached(timeout=WAIT_PAGE)


@pytest.mark.e2e
def test_full_timeline_from_the_live_card_detail_opens_the_card_page(
    page: Page, card_url: str, store: DashboardStore,
) -> None:
    _connected(page, card_url + "/")
    store.broadcaster.broadcast(_snapshot())
    expect(page.locator(f".swimlane-card[data-card-id='{CARD}']")).to_be_visible(timeout=_WAIT_LIVE)
    page.locator(f".swimlane-card[data-card-id='{CARD}']").click()
    expect(page.locator("#card-detail-view")).to_be_visible(timeout=_WAIT_LIVE)
    full = page.locator("#card-detail-content a", has_text="Full timeline")
    expect(full).to_have_attribute("href", f"/cards/{ALPHA}/{CARD}")
    full.click()
    expect(page).to_have_url(f"{card_url}/cards/{ALPHA}/{CARD}", timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section .tl-tracker")).to_be_attached(timeout=WAIT_PAGE)


# --- what the page says ----------------------------------------------------------- #


@pytest.mark.e2e
def test_an_unknown_card_says_no_timeline_is_recorded(page: Page, card_url: str) -> None:
    page.goto(f"{card_url}/cards/{ALPHA}/{UNKNOWN_CARD}")
    expect(page.locator("#card-page")).to_be_visible(timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section")).to_contain_text("No timeline recorded for this card", timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section .tl-tracker")).to_have_count(0)


@pytest.mark.e2e
def test_a_hostile_path_segment_is_shown_as_text_and_runs_nothing(page: Page, card_url: str) -> None:
    page.goto(f"{card_url}/cards/{ALPHA}/{HOSTILE}")
    expect(page.locator("#card-page-section")).to_contain_text("This is not a card address.", timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section img")).to_have_count(0)
    assert page.evaluate("typeof window.__pwned") == "undefined"


# --- history and the page's own address ------------------------------------------- #


@pytest.mark.e2e
def test_back_and_forward_move_between_the_history_table_and_the_card_page(
    page: Page, card_url: str, store: DashboardStore,
) -> None:
    link = _history_row_link(page, store, card_url, CARD)
    link.click()
    expect(page).to_have_url(f"{card_url}/cards/{ALPHA}/{CARD}", timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section .tl-tracker")).to_be_attached(timeout=WAIT_PAGE)
    page.go_back()
    expect(page).to_have_url(f"{card_url}/history", timeout=WAIT_PAGE)
    expect(page.locator("#history-page")).to_be_visible(timeout=WAIT_PAGE)
    expect(page.locator("#card-page")).to_be_hidden()
    page.go_forward()
    expect(page).to_have_url(f"{card_url}/cards/{ALPHA}/{CARD}", timeout=WAIT_PAGE)
    expect(page.locator("#card-page")).to_be_visible(timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section .tl-tracker")).to_be_attached(timeout=WAIT_PAGE)


# --- scope ------------------------------------------------------------------------- #


@pytest.mark.e2e
def test_a_viewer_scoped_to_another_symphony_sees_a_scoped_note_not_the_card(
    page: Page, card_url: str, store: DashboardStore,
) -> None:
    _connected(page, card_url + "/?symphony=" + ALPHA)
    store.broadcaster.broadcast(_snapshot())
    expect(page.locator("#symphony-select")).to_have_value(ALPHA, timeout=_WAIT_LIVE)
    page.evaluate("navigate(null, '/cards/" + BETA + "/" + BETA_CARD + "')")
    expect(page).to_have_url(re.compile(f"/cards/{BETA}/{BETA_CARD}"), timeout=WAIT_PAGE)
    section = page.locator("#card-page-section")
    expect(section).to_contain_text(NOTE_START, timeout=WAIT_PAGE)
    expect(section.locator(".tl-tracker")).to_have_count(0)


@pytest.mark.e2e
def test_choosing_all_symphonies_shows_the_scoped_card_on_the_card_page(
    page: Page, card_url: str, store: DashboardStore,
) -> None:
    _connected(page, card_url + "/?symphony=" + ALPHA)
    store.broadcaster.broadcast(_snapshot())
    expect(page.locator("#symphony-select")).to_have_value(ALPHA, timeout=_WAIT_LIVE)
    page.evaluate("navigate(null, '/cards/" + BETA + "/" + BETA_CARD + "')")
    expect(page.locator("#card-page-section")).to_contain_text(NOTE_START, timeout=WAIT_PAGE)
    page.locator("#symphony-select").select_option("")
    expect(page.locator("#card-page-section .tl-tracker")).to_be_attached(timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section")).not_to_contain_text(NOTE_START)



@pytest.mark.e2e
def test_a_direct_load_under_a_stored_choice_fetches_no_card_outside_it(
    page: Page, card_url: str,
) -> None:
    """A stored choice of alpha, then a direct load of beta's card. Nothing of beta's is fetched."""
    timeline_requests: list[str] = []
    page.on("request", lambda request: timeline_requests.append(request.url) if "/api/cards/" in request.url else None)
    page.add_init_script("window.localStorage.setItem('coordinare.symphony', 'alpha-sym')")
    page.goto(f"{card_url}/cards/{BETA}/{BETA_CARD}")
    expect(page.locator("#card-page")).to_be_visible(timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section")).to_contain_text(NOTE_START, timeout=WAIT_PAGE)
    expect(page.locator("#card-page-section .tl-tracker")).to_have_count(0)
    page.wait_for_timeout(500)
    assert timeline_requests == [], f"a timeline outside the stored choice was requested: {timeline_requests}"

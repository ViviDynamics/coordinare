"""#147: the card pipeline timeline in a real browser.

A card's tracker (a skipped stage greyed out), a stage's runs linking to their transcripts (133),
the lifecycle list, the board's dots, and a card of a symphony the viewer cannot see.

The daemon is a mock whose state is consistent with what the timeline reads: the card lifecycle
log, the transcript root, the symphony configs and each symphony's board and live sessions. The
page is driven by the first snapshot the server builds on connect, so the real snapshot builder
(including card_pipeline) is what the browser renders.
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

_WAIT_SSE = 3_000
_WAIT_LIVE = 8_000
_WAIT_RUN = 10_000
ALPHA = "alpha-sym"
BETA = "beta-sym"
CARD = "PVTI_A1"
BETA_CARD = "PVTI_B1"
RUN = "run147a1"
REVIEW_RUN = "run147r1"
BETA_RUN = "run147b1"
LIFECYCLE = ["assessing", "implementing", "reviewing", "closing_review"]
T0 = datetime(2026, 10, 14, 9, 0, 0, tzinfo=UTC)


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _header(run: str, card: str, persona: str) -> dict[str, Any]:
    return {
        "type": "run_header", "seq": 0, "header_version": 1, "ts": _at(0), "run_id": run,
        "persona": {"name": persona, "version": 2, "version_sha256": "ab" * 32,
                    "changed_at": _at(-3600), "changed_by": "local", "source": "dashboard"},
        "performer": {"id": "perf-1", "backend": "claude-code", "transport": "http", "endpoint": "ep-1"},
        "model": {"model": "claude-test-model"},
        "card": {"id": card, "issue_number": 147, "title": "Timeline"},
    }


def _write_run(root: Path, address: tuple[str, str], run: str, persona: str, length: float) -> None:
    """One run file in the writer's layout, for the card at ``address`` (its symphony and card id)."""
    symphony, card = address
    path = transcript_path(root, symphony=symphony, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        _header(run, card, persona),
        {"ts": _at(1), "seq": 1, "type": "user", "text": "Work on it."},
        {"ts": _at(length), "seq": 2, "type": "result", "is_error": False, "text": "Done."},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _alpha_log() -> CardLifecycleLog:
    log = CardLifecycleLog()
    log.record(ALPHA, CARD, "dispatched", stage="assessing", at=_at(0))
    log.record(ALPHA, CARD, "dispatched", stage="implementing", at=_at(60))
    log.record(ALPHA, CARD, "pr_opened", stage="implementing", at=_at(120),
               detail={"pr_number": 9, "pr_url": "https://github.com/acme/repo/pull/9"}, key="pr")
    log.record(ALPHA, CARD, "verdict", stage="reviewing", at=_at(150),
               detail={"result": "fail", "marker": "changes_requested", "head": "abc123"}, key="cr:abc")
    log.record(ALPHA, CARD, "dispatched", stage="reviewing", at=_at(200))
    return log


def _sym_config(number: int, repo: str) -> SimpleNamespace:
    return SimpleNamespace(github_project_number=number, github_org="acme", repo=repo)


def _sym_state(board: dict[str, list[str]], titles: dict[str, str], sessions: dict[str, Any]) -> SimpleNamespace:
    """What the daemon holds for one symphony: its board, its titles and its live sessions."""
    columns = {"TODO": [], "BLOCKED": [], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": [], "BACKLOG": []}
    columns.update(board)
    return SimpleNamespace(
        board_snapshot=columns, board_titles=titles, board_issue_numbers={}, board_issue_urls={},
        board_pr_urls={}, session_skip_reasons={}, todo_waits={}, todo_capacity=None,
        active_sessions=sessions, active_card=None, last_error=None, cycle_count=0, error_count=0,
        last_poll_at=None,
    )


@pytest.fixture(scope="module")
def timeline_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("timeline-transcripts")
    _write_run(root, (ALPHA, CARD), RUN, "implementer", 150.0)
    _write_run(root, (ALPHA, CARD), REVIEW_RUN, "reviewer", 30.0)
    _write_run(root, (BETA, BETA_CARD), BETA_RUN, "reviewer", 40.0)
    return root


@pytest.fixture(scope="module")
def timeline_daemon(timeline_root: Path) -> Any:
    log = _alpha_log()
    log.record(BETA, BETA_CARD, "dispatched", stage="reviewing", at=_at(0))
    # Each live session is in review, so its stage is the one in progress.
    alpha_session = {"performer_stage": "reviewing", "phase": "monitoring_pr",
                     "current_card": {"id": CARD, "title": "Alpha card"}}
    beta_session = {"performer_stage": "reviewing", "phase": "monitoring_pr",
                    "current_card": {"id": BETA_CARD, "title": "Beta card"}}
    daemon = _mock_daemon("monitoring_pr")
    daemon.state.update({
        "symphony_configs": {ALPHA: _sym_config(16, "alpha-repo"), BETA: _sym_config(2, "beta-repo")},
        "symphony_states": {
            ALPHA: _sym_state({"IN_REVIEW": [CARD]}, {CARD: "Alpha card"}, {CARD: alpha_session}),
            BETA: _sym_state({"IN_PROGRESS": [BETA_CARD]}, {BETA_CARD: "Beta card"}, {BETA_CARD: beta_session}),
        },
        "lifecycle_sequence": LIFECYCLE,
        "active_sessions": {CARD: alpha_session, BETA_CARD: beta_session},
        "card_lifecycle": log,
        "transcript_collector": TranscriptCollector(timeline_root, enabled=True, max_bytes_per_run=10**9,
                                                    retention_days=0),
    })
    return daemon


@pytest.fixture(scope="module")
def timeline_url(timeline_daemon: Any) -> Generator[str, None, None]:
    store = DashboardStore()
    port = _free_port()
    mark_daemon_started(timeline_daemon)
    app = create_dashboard_app(store, timeline_daemon, _mock_metrics(), _mock_health())
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


def _open(page: Page, base: str) -> None:
    """Load the dashboard. The first snapshot is the daemon's own, built on connect, so every view reads it."""
    page.goto(base + "/")
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=_WAIT_SSE)
    expect(page.locator(f".swimlane-card[data-card-id='{CARD}']")).to_be_visible(timeout=_WAIT_LIVE)


def _open_detail(page: Page, card: str) -> None:
    page.locator(f".swimlane-card[data-card-id='{card}']").click()
    expect(page.locator("#card-detail-view")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#card-detail-content .tl-tracker")).to_be_attached(timeout=_WAIT_RUN)


@pytest.mark.e2e
def test_the_tracker_lists_every_stage_and_greys_out_the_skipped_ones(page: Page, timeline_url: str) -> None:
    _open(page, timeline_url)
    _open_detail(page, CARD)
    expect(page.locator("#card-detail-content .tl-tracker .tl-stage")).to_have_count(8)
    architecting = page.locator("#card-detail-content .tl-stage[data-stage='architecting']")
    expect(architecting).to_have_class(re.compile(r"\btl-skipped\b"))
    expect(architecting).to_contain_text("Plan")
    expect(architecting).to_have_css("opacity", "0.5")
    implementing = page.locator("#card-detail-content .tl-stage[data-stage='implementing']")
    expect(implementing).to_have_class(re.compile(r"\btl-done\b"))
    expect(implementing).to_contain_text("Implement")
    expect(implementing).to_contain_text("1 run")
    expect(page.locator("#card-detail-content .tl-stage[data-stage='reviewing']")).to_have_class(re.compile(r"\btl-current\b"))


@pytest.mark.e2e
def test_clicking_a_stage_lists_its_runs_and_each_run_opens_its_transcript(page: Page, timeline_url: str) -> None:
    _open(page, timeline_url)
    _open_detail(page, CARD)
    page.locator("#card-detail-content .tl-stage[data-stage='implementing']").click()
    links = page.locator("#card-detail-content .tl-runs a")
    expect(links).to_have_count(1)
    expect(links.first).to_have_attribute("href", f"/runs/{ALPHA}/{CARD}/{RUN}")
    expect(page.locator("#card-detail-content .tl-detail")).to_contain_text("2m 30s")
    links.first.click()
    expect(page).to_have_url(re.compile(rf"/runs/{ALPHA}/{CARD}/{RUN}"), timeout=_WAIT_RUN)
    expect(page.locator("#runs-rows .run-row").first).to_be_attached(timeout=_WAIT_RUN)


@pytest.mark.e2e
def test_the_lifecycle_list_shows_the_recorded_events_in_order(page: Page, timeline_url: str) -> None:
    _open(page, timeline_url)
    _open_detail(page, CARD)
    events = page.locator("#card-detail-content .tl-events .tl-event")
    expect(events).to_have_count(5)
    expect(events.nth(0)).to_contain_text("Dispatched")
    expect(events.nth(1)).to_contain_text("Dispatched")
    expect(events.nth(2)).to_contain_text("Pull request opened #9")
    expect(events.nth(3)).to_contain_text("Verdict: changes requested")
    expect(events.nth(4)).to_contain_text("Dispatched")


@pytest.mark.e2e
def test_a_board_card_shows_one_dot_per_stage_with_the_current_one_ringed(page: Page, timeline_url: str) -> None:
    _open(page, timeline_url)
    card = page.locator(f".swimlane-card[data-card-id='{CARD}']")
    expect(card.locator(".tl-dots .tl-dot")).to_have_count(8)
    expect(card.locator(".tl-dots .tl-dot-current")).to_have_count(1)
    expect(card.locator(".tl-dots .tl-dot").nth(3)).to_have_class(re.compile(r"\btl-dot-current\b"))


@pytest.mark.e2e
def test_a_card_of_a_symphony_the_viewer_cannot_see_is_not_shown_and_not_found_under_this_one(
    page: Page, timeline_url: str,
) -> None:
    _open(page, timeline_url)
    page.locator("#symphony-select").select_option(ALPHA)
    expect(page.locator(f".swimlane-card[data-card-id='{BETA_CARD}']")).to_have_count(0)
    expect(page.locator(f".swimlane-card[data-card-id='{CARD}']")).to_be_visible()

    outside = page.request.get(f"{timeline_url}/api/cards/{ALPHA}/{BETA_CARD}/timeline")
    assert outside.status == 404
    inside = page.request.get(f"{timeline_url}/api/cards/{BETA}/{BETA_CARD}/timeline")
    assert inside.status == 200
    assert inside.json()["card_id"] == BETA_CARD

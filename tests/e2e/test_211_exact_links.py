"""#211: activity rows open the exact event, and the run header shows the ledger's cost, in a browser.

Chromium is the default. The same tests run in Firefox with ``--browser firefox``. The API is in
tests/unit/test_211_run_detail_api.py and the client helpers in tests/unit/test_211_client_js.py.
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.services.activity_log import EventLink
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
SYMPHONY = "coordinare"
ACT_CARD = "PVTI_211A"
ACT_RUN = "run-211-act"
OLD_CARD = "PVTI_211F"
MARKER_SEQ = 50
LAST_SEQ = 100
LATE_SEQ = 53
EARLY_SEQ = 20
PRICED_CARD = "PVTI_211P"
PRICED_RUN = "run-211-priced"
UNPRICED_CARD = "PVTI_211U"
UNPRICED_RUN = "run-211-unpriced"
PRICED_MODEL = "claude-opus-4-5"
UNPRICED_MODEL = "gpt-unpriced"
PRICING_AS_OF = date(2026, 10, 1)
PRICED_USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_read": 50, "cache_write": 10}
PRICED_TOKENS = "1,260 tokens"
PRICE_RATE_INPUT = 15.0
PRICE_RATE_OUTPUT = 75.0
PRICE_RATE_CACHE_READ = 1.5
PRICE_RATE_CACHE_WRITE = 18.75
# The ledger keeps only the usage window (8 UTC days) before today, so the run clock is the present.
BASE = datetime.now(UTC).replace(microsecond=0)


def _ts(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat()


def _header(run: str, card: str, model: str) -> dict[str, Any]:
    return {
        "type": "run_header", "seq": 0, "header_version": 1, "ts": _ts(0), "run_id": run,
        "persona": {"name": "implementing", "version": 3, "version_sha256": "cd" * 32,
                    "changed_at": _ts(-3600), "changed_by": "local", "source": "dashboard"},
        "performer": {"id": "perf-211", "backend": "claude-code", "transport": "http", "endpoint": "ep-211"},
        "model": {"model": model},
        "card": {"id": card, "issue_number": 211, "title": "Exact transcript links"},
    }


def _write(root: Path, card: str, run: str, lines: list[str]) -> None:
    path = transcript_path(root, symphony=SYMPHONY, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _usage_record(seq: int, model: str, counts: dict[str, int]) -> dict[str, Any]:
    return {"ts": _ts(seq), "seq": seq, "type": "usage", "scope": "message",
            "usage": {"model": model, **counts}}


def _activity_run(root: Path) -> None:
    lines = [json.dumps(_header(ACT_RUN, ACT_CARD, PRICED_MODEL))]
    for seq in range(1, LAST_SEQ + 1):
        text = "marker-50" if seq == MARKER_SEQ else f"event {seq}"
        lines.append(json.dumps({"ts": _ts(seq), "seq": seq, "type": "assistant_text", "text": text}))
    _write(root, ACT_CARD, ACT_RUN, lines)


def _usage_run(root: Path, card: str, run: str, model: str) -> list[dict[str, Any]]:
    usage = _usage_record(2, model, PRICED_USAGE)
    lines = [
        json.dumps(_header(run, card, model)),
        json.dumps({"ts": _ts(1), "seq": 1, "type": "user", "text": "Price this run."}),
        json.dumps(usage),
        json.dumps({"ts": _ts(3), "seq": 3, "type": "assistant_text", "text": "Done."}),
    ]
    _write(root, card, run, lines)
    return [usage]


@pytest.fixture(scope="module")
def transcript_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("transcripts-211")
    _activity_run(root)
    return root


@pytest.fixture(scope="module")
def store() -> DashboardStore:
    return DashboardStore()


@pytest.fixture(scope="module")
def ledger_server_url(store: DashboardStore, transcript_root: Path) -> Generator[str, None, None]:
    """A live server whose daemon names the transcript root, the ledger and the price table."""
    usage_records = {
        PRICED_RUN: _usage_run(transcript_root, PRICED_CARD, PRICED_RUN, PRICED_MODEL),
        UNPRICED_RUN: _usage_run(transcript_root, UNPRICED_CARD, UNPRICED_RUN, UNPRICED_MODEL),
    }
    collector = TranscriptCollector(transcript_root, enabled=True, max_bytes_per_run=10**9, retention_days=0)
    card_of = {PRICED_RUN: PRICED_CARD, UNPRICED_RUN: UNPRICED_CARD}
    for run, records in usage_records.items():
        collector.usage.observe(run, symphony=SYMPHONY, card=card_of[run], stage="implementing", records=records)
    price = SimpleNamespace(
        models=[PRICED_MODEL], input_per_million=PRICE_RATE_INPUT, output_per_million=PRICE_RATE_OUTPUT,
        cache_read_per_million=PRICE_RATE_CACHE_READ, cache_write_per_million=PRICE_RATE_CACHE_WRITE,
    )
    cost_tracking = SimpleNamespace(prices=[price], default_price=None, cost_per_million_tokens=None,
                                    pricing_as_of=PRICING_AS_OF)
    daemon = _mock_daemon()
    daemon.state["transcript_collector"] = collector
    daemon.state["config"] = SimpleNamespace(
        cost_tracking=cost_tracking, github_org="vividynamics", github_project_number=1,
    )
    port = _free_port()
    mark_daemon_started(daemon)
    app = create_dashboard_app(store, daemon, _mock_metrics(), _mock_health())
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


def _run_url(card: str, run: str, fragment: str = "") -> str:
    return f"/runs/{SYMPHONY}/{card}/{run}{fragment}"


@pytest.mark.e2e
def test_an_activity_row_opens_the_exact_event_it_was_built_from(
    page: Page, ledger_server_url: str, store: DashboardStore,
) -> None:
    # The event's own time is the time of a later record (53). Only the seq (50) can open the right event.
    store.activity_log.record(
        activity_type="progress", card_id=ACT_CARD, card_number=211, card_title="Exact transcript links",
        stage="implementing", text="running the viewer checks", session_id=ACT_RUN,
        link=EventLink(event_at=_ts(LATE_SEQ), transcript_seq=MARKER_SEQ),
    )
    page.goto(ledger_server_url + "/")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    link = page.locator(f"#activity-feed a.run-entry-link[href$='#e{MARKER_SEQ}']").first
    expect(link).to_be_attached(timeout=_WAIT_LIVE)
    expect(link).to_have_attribute("href", re.compile(rf"/runs/{ACT_RUN}#e{MARKER_SEQ}$"))
    link.click()
    expect(page.locator(f"#e{MARKER_SEQ}")).to_be_attached(timeout=_WAIT_RUN)
    expect(page.locator(f"#e{MARKER_SEQ}")).to_have_class(re.compile("run-focus"))
    expect(page.locator("#runs-rows")).to_contain_text("marker-50")


@pytest.mark.e2e
def test_an_entry_without_a_seq_falls_back_to_the_time_link(
    page: Page, ledger_server_url: str, store: DashboardStore,
) -> None:
    store.activity_log.record(
        activity_type="progress", card_id=OLD_CARD, card_number=212, card_title="Older performer",
        stage="implementing", text="an older performer's event", session_id=ACT_RUN,
        link=EventLink(event_at=_ts(EARLY_SEQ)),
    )
    page.goto(ledger_server_url + "/")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    link = page.locator(f"#activity-feed a.run-entry-link[href*='/runs/{ACT_RUN}?at=']").first
    expect(link).to_be_attached(timeout=_WAIT_LIVE)
    expect(link).to_have_attribute("href", re.compile(rf"/runs/{ACT_RUN}\?at="))
    link.click()
    expect(page.locator(f"#e{EARLY_SEQ}")).to_be_attached(timeout=_WAIT_RUN)


@pytest.mark.e2e
def test_the_header_shows_the_ledger_cost_with_its_breakdown(page: Page, ledger_server_url: str) -> None:
    page.goto(ledger_server_url + _run_url(PRICED_CARD, PRICED_RUN))
    header = page.locator("#runs-header")
    expect(header).to_contain_text(PRICED_TOKENS, timeout=_WAIT_RUN)
    expect(header).to_contain_text("$0.03")
    tooltip = header.locator(f"span[title*='Pricing as of {PRICING_AS_OF.isoformat()}']").first
    expect(tooltip).to_be_attached()
    title = tooltip.get_attribute("title") or ""
    assert "Cost: input $0.015000, output $0.015000" in title, title
    assert "cache read 50, cache write 10" in title, title
    assert f"{PRICED_MODEL}: 1,260 tokens" in title, title


@pytest.mark.e2e
def test_an_unpriced_model_says_unknown_and_why(page: Page, ledger_server_url: str) -> None:
    page.goto(ledger_server_url + _run_url(UNPRICED_CARD, UNPRICED_RUN))
    header = page.locator("#runs-header")
    expect(header).to_contain_text("1,260 tokens", timeout=_WAIT_RUN)
    cost = header.locator(f"span[title*='no price for {UNPRICED_MODEL}']").first
    expect(cost).to_have_text("unknown")
    assert "Cost unknown: no price for " + UNPRICED_MODEL in (cost.get_attribute("title") or "")
    assert "$0." not in header.inner_text()

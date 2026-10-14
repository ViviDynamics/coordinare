"""#131: the conductor's own model calls are listed on the card and open in the viewer.

The calls are written by the real wrapper (``ConductorCallLog``) into a temporary transcript
root. The card detail lists them next to its performer runs, and a call's link opens the
viewer at ``/runs/conductor/<card>/<call>``. A decision that is markup is shown as text.
"""

from __future__ import annotations

import asyncio
import re
import socket
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.services.conductor_calls import (
    ConductorCallLog,
    ModelReply,
    ModelRequest,
    conductor_call,
)
from coordinare.services.run_transcript import TranscriptCollector
from tests.e2e.conftest import _free_port, _mock_daemon, _mock_health, _mock_metrics
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_WAIT_RUN = 10_000
CARD = "PVTI_131"
MODEL = "claude-conductor-131"
ASSESS_DECISION = "assessed: ready"
SCOPE_DECISION = "scope: developer full"
HOSTILE_DECISION = '<img src=x onerror="window.__pwned = 131">'
_CARD_RUNS = "#card-detail-content .card-runs"


@dataclass(frozen=True)
class Live:
    url: str
    loop: asyncio.AbstractEventLoop
    store: DashboardStore


async def _write_calls(root: Path) -> dict[str, str]:
    """Three conductor calls on one card, written through the real wrapper. Returns their ids by kind."""
    log = ConductorCallLog(TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6, retention_days=0))
    ids: dict[str, str] = {}
    calls: list[tuple[str, str, str]] = [
        ("assess", ASSESS_DECISION, '{"sufficient": true}'),
        ("persona_scope", SCOPE_DECISION, '{"personas": {}}'),
        ("observer", HOSTILE_DECISION, '{"verdict": "continue"}'),
    ]
    for kind, decision, reply_text in calls:
        request = ModelRequest(model=MODEL, endpoint="anthropic_api", system="sys", prompt=f"{kind} prompt",
                               parameters={"max_tokens": 64})

        async def send(text: str = reply_text) -> ModelReply:
            return ModelReply(text=text, model=MODEL,
                              usage={"input_tokens": 90, "output_tokens": 12, "cache_read": 0, "cache_write": 0})

        async with conductor_call(log, kind=kind, card_id=CARD, symphony="coordinare") as call:
            await log.invoke(request, send)
            call.decide(decision)
        ids[kind] = call.call_id
    await log.drain()
    return ids


@pytest.fixture(scope="module")
def conductor_calls_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Writes the calls on a worker thread, so the wrapper's loop never meets Playwright's."""
    root = tmp_path_factory.mktemp("transcripts-131")
    box: list[dict[str, str]] = []
    thread = threading.Thread(target=lambda: box.append(asyncio.run(_write_calls(root))))
    thread.start()
    thread.join(timeout=30)
    assert box, "the conductor calls were not written"
    return root


@pytest.fixture(scope="module")
def conductor_live(conductor_calls_root: Path) -> Generator[Live, None, None]:
    """A live server whose daemon names the transcript root with the conductor calls in it."""
    store = DashboardStore()
    daemon = _mock_daemon()
    daemon.state["transcript_collector"] = TranscriptCollector(
        conductor_calls_root, enabled=True, max_bytes_per_run=10**9, retention_days=0,
    )
    port = _free_port()
    app = create_dashboard_app(store, daemon, _mock_metrics(), _mock_health())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    loop_box: list[asyncio.AbstractEventLoop] = []

    async def _serve() -> None:
        loop_box.append(asyncio.get_running_loop())
        await server.serve()

    thread = threading.Thread(target=lambda: asyncio.run(_serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not loop_box and time.monotonic() < deadline:
        time.sleep(0.02)
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
    yield Live(url=f"http://127.0.0.1:{port}", loop=loop_box[0], store=store)
    server.should_exit = True
    thread.join(timeout=5)


def _open_card_detail(page: Page, live: Live) -> None:
    page.goto(live.url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    snapshot = _full_snapshot(
        phase="monitoring_performer",
        phase_label="Monitoring Performer",
        agent_dispatch_at="2026-10-13T10:00:00+00:00",
        active_sessions=[_active_session(CARD, "Conductor calls", "implementing")],
    )
    live.loop.call_soon_threadsafe(live.store.broadcaster.broadcast, snapshot)
    row = page.locator(f"#swimlane-section .swimlane-card[data-card-id='{CARD}']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    row.click()
    expect(page.locator(_CARD_RUNS)).to_be_attached(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_card_detail_lists_the_conductor_calls_next_to_its_runs(page: Page, conductor_live: Live) -> None:
    _open_card_detail(page, conductor_live)
    runs = page.locator(_CARD_RUNS)
    expect(runs).to_contain_text(f"Conductor call: {ASSESS_DECISION}", timeout=_WAIT_RUN)
    expect(runs).to_contain_text(f"Conductor call: {SCOPE_DECISION}")
    expect(runs).to_contain_text("conductor:assess")


@pytest.mark.e2e
def test_a_conductor_call_opens_in_the_viewer(page: Page, conductor_live: Live, conductor_calls_root: Path) -> None:
    _open_card_detail(page, conductor_live)
    link = page.locator(_CARD_RUNS + " a", has_text=f"Conductor call: {ASSESS_DECISION}")
    expect(link).to_be_visible(timeout=_WAIT_RUN)
    link.click()
    run_id = _call_id_from_disk(conductor_calls_root, "assess")
    expect(page).to_have_url(re.compile(rf"/runs/conductor/{CARD}/{run_id}$"), timeout=_WAIT_RUN)
    expect(page.locator("#runs-header")).to_contain_text("conductor:assess", timeout=_WAIT_RUN)
    expect(page.locator("#runs-rows")).to_contain_text(ASSESS_DECISION, timeout=_WAIT_RUN)


@pytest.mark.e2e
def test_a_decision_that_is_markup_shows_as_text_on_the_card(page: Page, conductor_live: Live) -> None:
    _open_card_detail(page, conductor_live)
    runs = page.locator(_CARD_RUNS)
    expect(runs).to_contain_text("<img src=x", timeout=_WAIT_RUN)
    expect(page.locator("#card-detail-content img")).to_have_count(0)


def _call_id_from_disk(root: Path, kind: str) -> str:
    """The id of the call whose header names ``kind``, read from the file the wrapper wrote."""
    import json

    for path in (root / "conductor" / CARD).glob("*.jsonl"):
        header: Any = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        if header.get("conductor", {}).get("kind") == kind:
            return str(path.stem)
    pytest.fail(f"no conductor call of kind {kind} on disk")

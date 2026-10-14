"""#133: the transcript viewer in a real browser.

Each test serves a transcript root written into a temporary directory. The fixtures cover a
small run, a 2,000-event run and a hostile run. The API behaviour is in
tests/unit/test_133_run_viewer_api.py and the client helpers in tests/unit/test_133_runs_js.py.
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
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, Route, expect

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.services import run_viewer
from coordinare.services.run_transcript import TranscriptCollector, transcript_path
from tests.e2e.conftest import _free_port, _mock_daemon, _mock_health, _mock_metrics

_WAIT_SSE = 3_000
_WAIT_LIVE = 8_000
_WAIT_RUN = 10_000
SYMPHONY = "coordinare"
SMALL_CARD = "PVTI_133"
SMALL_RUN = "run-133"
BIG_CARD = "PVTI_BIG"
BIG_RUN = "run-big"
HOSTILE_CARD = "PVTI_HOSTILE"
HOSTILE_RUN = "run-hostile"
ACTIVITY_CARD = "PVTI_ACT"
ACTIVITY_RUN = "run-act"
BIG_EVENTS = 2000
BIG_ERROR_SEQ = 1500
ACTIVITY_SEQ = 50
ACTIVITY_TEXT = "running the viewer checks"
ACTIVITY_LAG_SECONDS = 3
FIRST_PAINT_BOUND_MS = 8_000
STEP_BOUND_MS = 1_500
DOM_ROW_BOUND = 200


def _ts(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).isoformat()


def _header(run: str, card: str, base: datetime) -> dict[str, Any]:
    return {
        "type": "run_header", "seq": 0, "header_version": 1, "ts": _ts(base, 0), "run_id": run,
        "persona": {"name": "implementing", "version": 3, "version_sha256": "cd" * 32,
                    "changed_at": _ts(base, -3600), "changed_by": "local", "source": "dashboard"},
        "performer": {"id": "perf-1", "backend": "claude-code", "transport": "http", "endpoint": "ep-1"},
        "model": {"model": "claude-test-model"},
        "card": {"id": card, "issue_number": 133, "title": "Transcript viewer"},
    }


def _write(root: Path, card: str, run: str, lines: list[str]) -> None:
    path = transcript_path(root, symphony=SYMPHONY, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _small_run(root: Path, base: datetime) -> None:
    records: list[dict[str, Any]] = [
        {"ts": _ts(base, 1), "seq": 1, "type": "user", "text": "Build the viewer."},
        {"ts": _ts(base, 2), "seq": 2, "type": "assistant_text",
         "text": "First line of prose.\nSecond line of prose."},
        {"ts": _ts(base, 3), "seq": 3, "type": "thinking", "text": "considering the layout"},
        {"ts": _ts(base, 4), "seq": 4, "type": "tool_call", "tool_name": "Bash", "call_id": "c1",
         "input": {"command": "pytest -q tests/"}},
        {"ts": _ts(base, 6.3), "seq": 5, "type": "tool_result", "call_id": "c1", "is_error": True,
         "output": {"exit_code": 1, "stdout": "1 failed, 40 passed"}},
        {"ts": _ts(base, 7), "seq": 6, "type": "usage", "scope": "message",
         "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read": 8, "cache_write": 2}},
        {"ts": _ts(base, 8), "seq": 7, "type": "result", "is_error": False,
         "text": "Done. https://github.com/vividynamics/coordinare/pull/9"},
    ]
    lines = [json.dumps(_header(SMALL_RUN, SMALL_CARD, base))] + [json.dumps(r) for r in records]
    _write(root, SMALL_CARD, SMALL_RUN, lines)


def _big_run(root: Path, base: datetime) -> None:
    lines = [json.dumps(_header(BIG_RUN, BIG_CARD, base))]
    for k in range(1, BIG_EVENTS // 2 + 1):
        call_seq = 2 * k - 1
        result_seq = 2 * k
        lines.append(json.dumps({"ts": _ts(base, call_seq), "seq": call_seq, "type": "tool_call",
                                 "tool_name": "Bash", "call_id": f"c{k}",
                                 "input": {"command": f"step {k}"}}))
        failed = result_seq == BIG_ERROR_SEQ
        lines.append(json.dumps({"ts": _ts(base, result_seq), "seq": result_seq, "type": "tool_result",
                                 "call_id": f"c{k}", "is_error": failed,
                                 "output": {"exit_code": 1 if failed else 0, "stdout": f"output {k}"}}))
    _write(root, BIG_CARD, BIG_RUN, lines)


def _hostile_run(root: Path, base: datetime) -> None:
    script = "<script>window.__pwned = 1</script>"
    records: list[dict[str, Any]] = [
        {"ts": _ts(base, 1), "seq": 1, "type": "user", "text": script},
        {"ts": _ts(base, 2), "seq": 2, "type": "assistant_text",
         "text": '<img src=x onerror="window.__pwned = 2"> <a href="javascript:window.__pwned=3">go</a>'},
        {"ts": _ts(base, 3), "seq": 3, "type": "tool_call", "tool_name": "<b onmouseover=\"window.__pwned = 4\">Bash</b>",
         "call_id": "h1", "input": {"command": script,
                                    "file_path": '"><img src=x onerror="window.__pwned = 5">.py'}},
        {"ts": _ts(base, 4), "seq": 4, "type": "tool_result", "call_id": "h1", "is_error": False,
         "output": script},
    ]
    lines = [json.dumps(_header(HOSTILE_RUN, HOSTILE_CARD, base))] + [json.dumps(r) for r in records]
    _write(root, HOSTILE_CARD, HOSTILE_RUN, lines)


def _activity_run(root: Path, activity_at: datetime) -> None:
    """Records whose seq 50 is stamped at the moment the activity entry was recorded."""
    lines = [json.dumps(_header(ACTIVITY_RUN, ACTIVITY_CARD, activity_at))]
    for seq in range(1, 101):
        text = "marker-50" if seq == ACTIVITY_SEQ else f"event {seq}"
        lines.append(json.dumps({"ts": _ts(activity_at, seq - ACTIVITY_SEQ), "seq": seq,
                                 "type": "assistant_text", "text": text}))
    _write(root, ACTIVITY_CARD, ACTIVITY_RUN, lines)


HUGE_RUN = "run-huge"
HUGE_EVENTS = 20_001


def _huge_run(root: Path, base: datetime) -> None:
    lines = [json.dumps(_header(HUGE_RUN, "PVTI_HUGE", base))]
    for seq in range(1, HUGE_EVENTS + 1):
        lines.append(json.dumps({"ts": _ts(base, seq * 0.001), "seq": seq, "type": "assistant_text",
                                 "text": f"huge event {seq}"}))
    _write(root, "PVTI_HUGE", HUGE_RUN, lines)


@pytest.fixture(scope="module")
def transcript_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("transcripts")
    base = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
    _small_run(root, base)
    _big_run(root, base)
    _hostile_run(root, base)
    _huge_run(root, base)
    return root


@pytest.fixture(scope="module")
def store() -> DashboardStore:
    return DashboardStore()


@pytest.fixture(scope="module")
def run_server_url(store: DashboardStore, transcript_root: Path) -> Generator[str, None, None]:
    """A live server whose daemon state names the transcript root. Started once per module."""
    daemon = _mock_daemon()
    daemon.state["transcript_collector"] = TranscriptCollector(
        transcript_root, enabled=True, max_bytes_per_run=10**9, retention_days=0,
    )
    port = _free_port()
    app = create_dashboard_app(store, daemon, _mock_metrics(), _mock_health())
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: _serve(server), daemon=True)
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


def _serve(server: uvicorn.Server) -> None:
    asyncio.run(server.serve())


def _run_url(card: str, run: str, fragment: str = "") -> str:
    return f"/runs/{SYMPHONY}/{card}/{run}{fragment}"


def _open_run(page: Page, base: str, card: str, run: str, fragment: str = "") -> None:
    page.goto(base + _run_url(card, run, fragment))
    expect(page.locator("#runs-rows .run-row").first).to_be_attached(timeout=_WAIT_RUN)


@pytest.mark.e2e
def test_a_deep_link_opens_the_run_at_the_event_in_its_fragment(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, BIG_CARD, BIG_RUN, "#e1500")
    row = page.locator("#e1500")
    expect(row).to_be_attached(timeout=_WAIT_RUN)
    expect(row).to_contain_text("exit 1")
    expect(page.locator("#runs-header")).to_contain_text("Stage")
    expect(page.locator("#runs-header")).to_contain_text("implementing")


@pytest.mark.e2e
def test_a_tool_row_is_one_line_and_expands_to_its_full_input_and_output(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, SMALL_CARD, SMALL_RUN)
    summary = page.locator("#runs-rows details.run-tool summary").first
    expect(summary).to_have_text("Bash · pytest -q tests/ · exit 1 · 2.3s", timeout=_WAIT_RUN)
    details = page.locator("#runs-rows details.run-tool").first
    expect(details).not_to_have_attribute("open", "")
    summary.click()
    expect(details).to_have_attribute("open", "")
    expect(details).to_contain_text("pytest -q tests/")
    page.locator("#runs-rows details.run-result summary").first.click()
    expect(page.locator("#runs-rows")).to_contain_text("1 failed, 40 passed")


@pytest.mark.e2e
def test_thinking_is_collapsed_by_default(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, SMALL_CARD, SMALL_RUN)
    thinking = page.locator("#runs-rows details.run-think").first
    expect(thinking).to_be_attached(timeout=_WAIT_RUN)
    expect(thinking).not_to_have_attribute("open", "")


@pytest.mark.e2e
def test_the_type_filter_and_the_errors_filter_narrow_the_run(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, SMALL_CARD, SMALL_RUN)
    expect(page.locator("#runs-notice")).to_contain_text("7 of 7 records", timeout=_WAIT_RUN)
    page.locator("#runs-type-filter").select_option("tool_call")
    expect(page.locator("#runs-notice")).to_contain_text("1 of 7 records", timeout=_WAIT_LIVE)
    expect(page.locator("#runs-rows .run-row")).to_have_count(1)
    page.locator("#runs-type-filter").select_option("")
    page.locator("#runs-errors-filter").check()
    expect(page.locator("#runs-rows")).to_contain_text("exit 1", timeout=_WAIT_LIVE)
    expect(page.locator("#runs-rows .run-row")).to_have_count(1)


@pytest.mark.e2e
def test_next_error_jumps_to_the_failed_result(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, BIG_CARD, BIG_RUN)
    page.locator('[data-action="next-error"]').click()
    expect(page.locator(f"#e{BIG_ERROR_SEQ}")).to_be_attached(timeout=_WAIT_LIVE)
    expect(page.locator(f"#e{BIG_ERROR_SEQ}")).to_contain_text("exit 1")


@pytest.mark.e2e
def test_search_finds_an_event_and_jumps_to_it(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, BIG_CARD, BIG_RUN)
    page.locator("#runs-search").fill("step 900")
    page.locator("#runs-search-form button[type=submit]").click()
    match = page.locator("#runs-search-results .run-match").first
    expect(match).to_contain_text("step 900", timeout=_WAIT_LIVE)
    match.click()
    expect(page.locator("#runs-rows")).to_contain_text("step 900", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_copy_link_puts_the_event_address_on_the_clipboard(page: Page, run_server_url: str) -> None:
    # Record what the page writes instead of reading the real clipboard: Firefox has no
    # clipboard-read permission to grant, and the test is about the address written.
    page.add_init_script(
        """(() => {
            window.__copied = null;
            const clip = { writeText: (text) => { window.__copied = text; return Promise.resolve(); } };
            Object.defineProperty(navigator, 'clipboard', { value: clip, configurable: true });
        })();""",
    )
    _open_run(page, run_server_url, SMALL_CARD, SMALL_RUN)
    page.locator('#e2 [data-action="copy"]').click()
    expect(page.locator("#runs-notice")).to_contain_text("Link copied", timeout=_WAIT_LIVE)
    copied = page.evaluate("window.__copied")
    assert copied.endswith(_run_url(SMALL_CARD, SMALL_RUN, "#e2")), copied


@pytest.mark.e2e
def test_a_short_link_resolves_to_the_full_address(page: Page, run_server_url: str) -> None:
    page.goto(f"{run_server_url}/runs/{SMALL_RUN}")
    expect(page.locator("#runs-rows .run-row").first).to_be_attached(timeout=_WAIT_RUN)
    expect(page).to_have_url(f"{run_server_url}{_run_url(SMALL_CARD, SMALL_RUN)}")


@pytest.mark.e2e
def test_an_unknown_run_says_it_was_not_found(page: Page, run_server_url: str) -> None:
    page.goto(f"{run_server_url}/runs/{SYMPHONY}/{SMALL_CARD}/run-nope")
    expect(page.locator("#runs-header")).to_contain_text("Run not found", timeout=_WAIT_RUN)


@pytest.mark.e2e
def test_the_persona_diff_control_reports_a_version_that_is_not_retained(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, SMALL_CARD, SMALL_RUN)
    page.locator('[data-action="diff"]').click()
    expect(page.locator("#runs-diff")).to_contain_text("no longer retained", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_2000_event_run_stays_responsive_and_every_event_is_reachable(
    page: Page, run_server_url: str,
) -> None:
    started = time.monotonic()
    page.goto(run_server_url + _run_url(BIG_CARD, BIG_RUN))
    expect(page.locator("#runs-rows .run-row").first).to_be_attached(timeout=_WAIT_RUN)
    first_paint_ms = (time.monotonic() - started) * 1000
    assert first_paint_ms < FIRST_PAINT_BOUND_MS, f"first paint took {first_paint_ms:.0f} ms"
    assert page.locator("#runs-rows .run-row").count() < DOM_ROW_BOUND

    scroller = page.locator("#runs-scroll")
    scroller.focus()
    for _ in range(10):
        step_started = time.monotonic()
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(50)
        step_ms = (time.monotonic() - step_started) * 1000
        assert step_ms < STEP_BOUND_MS, f"a scroll step took {step_ms:.0f} ms"
        assert page.locator("#runs-rows .run-row").count() < DOM_ROW_BOUND

    for _ in range(60):
        page.evaluate("() => { const s = document.getElementById('runs-scroll'); s.scrollTop = s.scrollHeight; }")
        page.wait_for_timeout(100)
        if f"{BIG_EVENTS} of {BIG_EVENTS}" in (page.locator("#runs-notice").text_content() or ""):
            break
    expect(page.locator("#runs-notice")).to_contain_text(f"{BIG_EVENTS} of {BIG_EVENTS}", timeout=_WAIT_RUN)
    page.evaluate("() => { const s = document.getElementById('runs-scroll'); s.scrollTop = s.scrollHeight; }")
    expect(page.locator(f"#e{BIG_EVENTS}")).to_be_attached(timeout=_WAIT_RUN)


@pytest.mark.e2e
def test_a_hostile_transcript_renders_as_text_and_runs_nothing(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, HOSTILE_CARD, HOSTILE_RUN)
    expect(page.locator("#runs-rows")).to_contain_text("window.__pwned = 1", timeout=_WAIT_RUN)
    expect(page.locator("#runs-rows")).to_contain_text("<script>window.__pwned = 1</script>")
    assert page.locator("#runs-rows script").count() == 0
    assert page.locator("#runs-rows img").count() == 0
    assert page.locator("#runs-rows a[href^='javascript']").count() == 0
    assert page.locator("#runs-rows [onmouseover]").count() == 0
    assert page.evaluate("typeof window.__pwned") == "undefined"


@pytest.mark.e2e
def test_an_activity_entry_links_to_the_event_it_was_recorded_at(
    page: Page, run_server_url: str, store: DashboardStore, transcript_root: Path,
) -> None:
    # The performer emitted the event at event_at; the daemon observed it ACTIVITY_LAG seconds later.
    # The link must open the event (seq 50), not the time the daemon observed it (which is seq 53).
    event_at = datetime.now(UTC) - timedelta(seconds=ACTIVITY_LAG_SECONDS)
    store.activity_log.record(
        activity_type="progress", card_id=ACTIVITY_CARD, card_number=133, card_title="Transcript viewer",
        stage="implementing", text=ACTIVITY_TEXT, session_id=ACTIVITY_RUN, event_at=event_at.isoformat(),
    )
    _activity_run(transcript_root, event_at)
    page.goto(run_server_url + "/")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    link = page.locator("#activity-feed a.run-entry-link").first
    expect(link).to_be_attached(timeout=_WAIT_LIVE)
    link.click()
    expect(page.locator(f"#e{ACTIVITY_SEQ}")).to_be_attached(timeout=_WAIT_RUN)
    expect(page.locator("#runs-rows")).to_contain_text("marker-50")
    expect(page.locator(f"#e{ACTIVITY_SEQ}")).to_have_class(re.compile("run-focus"))


@pytest.mark.e2e
def test_a_link_to_an_event_that_does_not_exist_says_so(page: Page, run_server_url: str) -> None:
    page.goto(run_server_url + _run_url(SMALL_CARD, SMALL_RUN) + "#e999")
    expect(page.locator("#runs-notice")).to_contain_text("No event matches that link", timeout=_WAIT_RUN)
    expect(page.locator("#runs-rows .run-row").first).to_be_attached()


@pytest.mark.e2e
def test_a_failed_records_request_does_not_freeze_paging(page: Page, run_server_url: str) -> None:
    failed = {"done": False}

    def _fail_once(route: Route) -> None:
        if not failed["done"]:
            failed["done"] = True
            route.abort()
        else:
            route.continue_()

    page.route("**/records?**", _fail_once)
    page.goto(run_server_url + _run_url(BIG_CARD, BIG_RUN))
    expect(page.locator("#runs-notice")).to_contain_text("could not be loaded", timeout=_WAIT_RUN)
    page.locator("#runs-type-filter").select_option("tool_result")
    expect(page.locator("#runs-notice")).to_contain_text("of 2000 records", timeout=_WAIT_LIVE)
    page.unroute("**/records?**")


@pytest.mark.e2e
def test_a_search_stopped_at_its_byte_budget_offers_more_matches(
    page: Page, run_server_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_viewer, "SEARCH_BYTE_BUDGET", 40_000)
    _open_run(page, run_server_url, BIG_CARD, BIG_RUN)
    page.locator("#runs-search").fill("step 900")
    page.locator("#runs-search-form button[type=submit]").click()
    expect(page.locator("#runs-search-results")).to_contain_text("stopped after", timeout=_WAIT_LIVE)
    more = page.locator('#runs-search-results [data-action="more-matches"]')
    for _ in range(200):
        if page.locator("#runs-search-results .run-match", has_text="step 900").count():
            break
        if more.count() == 0:
            break
        more.click()
        page.wait_for_timeout(100)
    expect(page.locator("#runs-search-results .run-match", has_text="step 900")).to_be_attached(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_seq_typed_into_the_box_reaches_that_event(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, BIG_CARD, BIG_RUN)
    page.locator("#runs-goto").fill("1501")
    page.locator("#runs-goto-form button[type=submit]").click()
    expect(page.locator("#e1501")).to_be_attached(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_every_event_of_a_run_past_the_client_cap_is_reachable(page: Page, run_server_url: str) -> None:
    _open_run(page, run_server_url, "PVTI_HUGE", HUGE_RUN, f"#e{HUGE_EVENTS}")
    expect(page.locator(f"#e{HUGE_EVENTS}")).to_be_attached(timeout=_WAIT_RUN)
    assert page.locator("#runs-rows .run-row").count() < DOM_ROW_BOUND
    page.locator("#runs-goto").fill("1")
    page.locator("#runs-goto-form button[type=submit]").click()
    expect(page.locator("#e1")).to_be_attached(timeout=_WAIT_RUN)

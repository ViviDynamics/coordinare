"""#133 review fixes: Markdown escaping and speed, naive times, search budget and cursor,
backward paging, the not-found notice, the summary cache identity, and the activity time.

Each test here was failing before the fix it covers.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.localhost_guard import build_permitted
from coordinare.services import run_viewer
from coordinare.services.activity_log import ActivityLog, EventLink
from coordinare.services.run_transcript import TranscriptCollector, transcript_path

BASE = "http://127.0.0.1:8090"
SYMPHONY = "coordinare"
CARD = "PVTI_133"
RUN = "run-133"
HOSTILE_PERSONA = "impl [evil](https://evil.example)"
HOSTILE_TOOL = "[click](https://evil.example/x) <img src=x onerror=alert(1)>"
HOSTILE_KIND = "user*<img src=x>"
LINE_CHUNK_BYTES = 64 * 1024
LARGE_RECORDS = 20_000
SMALL_RECORDS = 2_000


def _header(ts: str = "2026-10-01T10:00:00+00:00", persona: str = "implementing") -> dict[str, object]:
    return {
        "type": "run_header", "seq": 0, "header_version": 1, "ts": ts, "run_id": RUN,
        "persona": {"name": persona, "version": 3, "version_sha256": "ab" * 32},
        "performer": {"id": "p1", "backend": "claude-code", "transport": "http", "endpoint": "e1"},
        "model": {"model": "m1"},
        "card": {"id": CARD, "issue_number": 133, "title": "T"},
    }


def _write(root: Path, records: list[dict[str, object]], header: dict[str, object] | None = None,
           extra: list[str] | None = None) -> Path:
    path = transcript_path(root, symphony=SYMPHONY, card_id=CARD, run_id=RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [json.dumps(header if header is not None else _header())]
    body += [json.dumps(r) for r in records]
    body += extra or []
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _client(root: Path) -> TestClient:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0,
                    "transcript_collector": TranscriptCollector(root, enabled=True,
                                                                max_bytes_per_run=10**9, retention_days=0)}
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    return TestClient(app, base_url=BASE)


def _ref() -> run_viewer.RunRef:
    return run_viewer.RunRef(SYMPHONY, CARD, RUN)


def _markdown(path: Path) -> str:
    with run_viewer.open_run(path, _ref()) as handle:
        return "".join(run_viewer.iter_markdown(handle, _ref()))


def _outside_code(markdown: str) -> str:
    """The Markdown that renders as prose: fenced blocks and backtick spans removed."""
    kept: list[str] = []
    fenced = False
    fence_marker = ""
    for line in markdown.splitlines():
        stripped = line.lstrip()
        if not fenced and stripped.startswith("```"):
            opening = re.match(r"`+", stripped)
            assert opening is not None
            fence_marker = opening.group(0)
            fenced = True
            continue
        if fenced:
            if stripped.startswith(fence_marker) and not stripped.strip("`"):
                fenced = False
            continue
        kept.append(re.sub(r"(`+)(.+?)\1", "", line))
    return "\n".join(kept)


# --- 1. Markdown escaping ---------------------------------------------------- #


def test_hostile_header_values_do_not_render_as_links_or_markup(tmp_path: Path) -> None:
    header = _header(persona=HOSTILE_PERSONA)
    header["model"] = {"model": "m1 [x](https://evil.example)\n# injected heading"}
    header["card"] = {"id": CARD, "issue_number": 133, "title": "<img src=x onerror=alert(2)>\n## heading"}
    path = _write(tmp_path, [{"seq": 1, "type": "user", "text": "hi"}], header=header)
    prose = _outside_code(_markdown(path))
    assert "](https://evil.example" not in prose
    assert "<img" not in prose
    assert "# injected" not in prose
    assert "## heading" not in prose


def test_hostile_tool_name_kind_and_persona_stay_inside_code_spans(tmp_path: Path) -> None:
    records = [
        {"seq": 1, "type": HOSTILE_KIND, "text": "k"},
        {"seq": 2, "type": "tool_call", "tool_name": HOSTILE_TOOL, "call_id": "c", "input": {"command": "ls"}},
    ]
    path = _write(tmp_path, records)
    body = _markdown(path)
    prose = _outside_code(body)
    assert "](https://evil.example" not in prose
    assert "<img" not in prose
    assert "user*<img" not in prose
    assert "`[click](https://evil.example/x) <img src=x onerror=alert(1)>`" in body


def test_a_value_with_backticks_gets_a_longer_code_span(tmp_path: Path) -> None:
    records = [{"seq": 1, "type": "tool_call", "tool_name": "a``b", "call_id": "c", "input": {}}]
    body = _markdown(_write(tmp_path, records))
    assert "```a``b```" in body


def test_fenced_bodies_outgrow_any_backtick_run_in_their_content(tmp_path: Path) -> None:
    records = [{"seq": 1, "type": "assistant_text", "text": "x ``````` y"}]
    body = _markdown(_write(tmp_path, records))
    assert "````````text" in body


def test_newlines_in_a_header_value_cannot_start_a_new_markdown_line(tmp_path: Path) -> None:
    header = _header(persona="a\n# owned")
    body = _markdown(_write(tmp_path, [{"seq": 1, "type": "user", "text": "x"}], header=header))
    assert not any(line.startswith("# owned") for line in body.splitlines())


# --- 2. Markdown speed -------------------------------------------------------- #


def _many(count: int) -> list[dict[str, object]]:
    return [{"seq": i, "type": "assistant_text", "text": f"event {i} " + "w" * 40} for i in range(1, count + 1)]


def test_markdown_is_yielded_in_large_chunks_not_per_record(tmp_path: Path) -> None:
    path = _write(tmp_path, _many(LARGE_RECORDS))
    with run_viewer.open_run(path, _ref()) as handle:
        chunks = list(run_viewer.iter_markdown(handle, _ref()))
    total = sum(len(c.encode("utf-8")) for c in chunks)
    assert len(chunks) <= total // LINE_CHUNK_BYTES + 2, (len(chunks), total)


def test_markdown_time_scales_linearly_with_records(tmp_path: Path) -> None:
    small = _write(tmp_path / "small", _many(SMALL_RECORDS))
    large = _write(tmp_path / "large", _many(LARGE_RECORDS))

    def timed(path: Path) -> float:
        started = time.perf_counter()
        with run_viewer.open_run(path, _ref()) as handle:
            for _ in run_viewer.iter_markdown(handle, _ref()):
                pass
        return time.perf_counter() - started

    timed(small)
    ratio = timed(large) / max(timed(small), 1e-6)
    # Linear scaling is 10x for 10x the records. Allow generous noise, reject quadratic growth.
    assert ratio < 25, ratio


def test_a_20000_record_markdown_download_is_not_per_record_through_the_app(tmp_path: Path) -> None:
    _write(tmp_path, _many(LARGE_RECORDS))
    client = _client(tmp_path)
    started = time.perf_counter()
    response = client.get(f"/api/runs/{SYMPHONY}/{CARD}/{RUN}/download.md")
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert response.text.count("### ") >= LARGE_RECORDS
    assert elapsed < 20, elapsed


# --- 4. naive timestamps ----------------------------------------------------- #


def test_a_naive_header_timestamp_does_not_break_the_run_detail(tmp_path: Path) -> None:
    records = [{"seq": 1, "type": "user", "ts": "2026-10-01T10:00:05+00:00", "text": "a"},
               {"seq": 2, "type": "result", "ts": "2026-10-01T10:00:09+00:00", "is_error": False, "text": "done"}]
    _write(tmp_path, records, header=_header(ts="2026-10-01T10:00:00"))
    response = _client(tmp_path).get(f"/api/runs/{SYMPHONY}/{CARD}/{RUN}")
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["duration_seconds"] == 9.0


def test_a_naive_at_parameter_is_read_as_utc(tmp_path: Path) -> None:
    _write(tmp_path, [{"seq": 1, "type": "user", "ts": "2026-10-01T10:00:00+00:00", "text": "a"},
                      {"seq": 2, "type": "user", "ts": "2026-10-01T10:00:09+00:00", "text": "b"}])
    response = _client(tmp_path).get(
        f"/api/runs/{SYMPHONY}/{CARD}/{RUN}/records", params={"at": "2026-10-01T10:00:05"},
    )
    assert response.status_code == 200
    assert response.json()["focus_line"] == 3


# --- 5. search budget and cursor ---------------------------------------------- #


def test_a_search_stops_at_its_byte_budget_and_the_cursor_continues_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [{"seq": i, "type": "assistant_text", "text": f"needle {i} " + "p" * 200} for i in range(1, 41)]
    path = _write(tmp_path, records)
    monkeypatch.setattr(run_viewer, "SEARCH_BYTE_BUDGET", 2_000)
    seen: list[int] = []
    after = 0
    stops = 0
    for _ in range(100):
        with run_viewer.open_run(path, _ref()) as handle:
            page = run_viewer.search(
                handle, query="needle", record_filter=run_viewer.RecordFilter(), after=after, limit=200,
            )
        seen += [m["seq"] for m in page.records]
        if page.stopped_at_byte is not None:
            stops += 1
        if page.next_after is None:
            break
        after = page.next_after
    assert stops >= 1
    assert seen == list(range(1, 41))


def test_the_search_endpoint_reports_the_stop_and_the_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = [{"seq": i, "type": "assistant_text", "text": "needle " + "p" * 500} for i in range(1, 30)]
    _write(tmp_path, records)
    monkeypatch.setattr(run_viewer, "SEARCH_BYTE_BUDGET", 3_000)
    body = _client(tmp_path).get(f"/api/runs/{SYMPHONY}/{CARD}/{RUN}/search", params={"q": "needle"}).json()
    assert body["stopped_at_byte"] is not None
    assert body["next_after"] is not None


def test_search_slots_bound_concurrent_scans() -> None:
    assert run_viewer.SEARCH_CONCURRENCY >= 1
    assert isinstance(run_viewer._SEARCH_SLOTS, threading.BoundedSemaphore)


# --- 6/7. backward paging ----------------------------------------------------- #


def test_records_before_a_line_page_backwards_in_order(tmp_path: Path) -> None:
    _write(tmp_path, _many(30))
    body = _client(tmp_path).get(
        f"/api/runs/{SYMPHONY}/{CARD}/{RUN}/records", params={"before": 21, "limit": 5},
    ).json()
    # Line 21 is seq 20, so the lines before it hold seqs 1 to 19; the last five are 15 to 19.
    assert [r["seq"] for r in body["records"]] == [15, 16, 17, 18, 19]
    assert body["prev_before"] == 16  # the line of seq 15; more events precede it


def test_records_before_the_start_have_no_further_cursor(tmp_path: Path) -> None:
    _write(tmp_path, _many(3))
    body = _client(tmp_path).get(
        f"/api/runs/{SYMPHONY}/{CARD}/{RUN}/records", params={"before": 3, "limit": 50},
    ).json()
    assert [r["seq"] for r in body["records"]] == [1]
    assert body["prev_before"] is None


# --- 8. a link that matches nothing says so ---------------------------------- #


def test_a_seq_that_matches_nothing_reports_focus_not_found(tmp_path: Path) -> None:
    _write(tmp_path, _many(5))
    body = _client(tmp_path).get(f"/api/runs/{SYMPHONY}/{CARD}/{RUN}/records", params={"seq": 999}).json()
    assert body["focus_found"] is False


def test_an_at_that_matches_nothing_reports_focus_not_found(tmp_path: Path) -> None:
    _write(tmp_path, _many(5))
    body = _client(tmp_path).get(
        f"/api/runs/{SYMPHONY}/{CARD}/{RUN}/records", params={"at": "2099-01-01T00:00:00+00:00"},
    ).json()
    assert body["focus_found"] is False


# --- 10. summary cache identity ----------------------------------------------- #


def test_a_replaced_file_with_the_same_size_and_mtime_is_not_served_from_cache(tmp_path: Path) -> None:
    path = _write(tmp_path, [{"seq": 1, "type": "result", "is_error": False, "text": "aaaa"}])
    before = path.stat()
    with run_viewer.open_run(path, _ref()) as handle:
        first = run_viewer._summary_for(path, handle)
    replacement = path.with_name("replacement.tmp")
    replacement.write_text(path.read_text(encoding="utf-8").replace("aaaa", "bbbb"), encoding="utf-8")
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, path)
    with run_viewer.open_run(path, _ref()) as handle:
        second = run_viewer._summary_for(path, handle)
    assert first is not second
    assert second.result_text == "bbbb"


# --- 3. activity entries carry the performer's event time ---------------------- #


def test_an_activity_entry_keeps_the_performer_event_time_beside_the_daemon_time() -> None:
    log = ActivityLog()
    entry = log.record(
        activity_type="tool_use", card_id=CARD, text="Bash", summary="Bash · ls",
        link=EventLink(event_at="2026-10-01T10:00:04+00:00"),
    )
    assert entry is not None
    body = log.snapshot()[-1]
    assert body["event_at"] == "2026-10-01T10:00:04+00:00"
    assert body["timestamp"] != body["event_at"]


def test_the_monitor_records_each_backend_events_own_time_on_its_entry() -> None:
    from coordinare.graph.nodes.monitor.activity import _record_activity_batch

    log = ActivityLog()
    state = {"activity_log": log, "agent_dispatch": {"session_id": "run-1", "performer_id": "p"}}
    events = [{"type": "tool_use", "tool": "Bash", "arg": "ls", "timestamp": "2026-10-01T10:00:04+00:00"}]
    _record_activity_batch(cast("Any", state), events, card_id=CARD, stage="implementing")
    assert log.snapshot()[-1]["event_at"] == "2026-10-01T10:00:04+00:00"

"""#133: the transcript viewer's read API. Auth, paging, traversal and link refusal, unparsable
lines, previews versus full fields, downloads, search, and the missing-run cases.

The browser behaviour is in tests/e2e/test_133_transcript_viewer.py. The client helpers are
in tests/unit/test_133_runs_js.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.localhost_guard import build_permitted
from coordinare.mcp.tools import is_deferred
from coordinare.services import run_viewer
from coordinare.services.run_transcript import TranscriptCollector, transcript_path

BASE = "http://127.0.0.1:8090"
TOKEN = "a-secret-operator-token-of-at-least-32-characters"
SYMPHONY = "coordinare"
CARD = "PVTI_133"
RUN = "run-133"
OTHER_RUN = "run-134"
BIG_CHARS = 5000
SMALL_PAGE = 2


def _header() -> dict[str, object]:
    return {
        "type": "run_header", "seq": 0, "header_version": 1, "ts": "2026-10-01T10:00:00+00:00",
        "run_id": RUN,
        "persona": {"name": "implementing", "version": 3, "version_sha256": "ab" * 32,
                    "changed_at": "2026-09-30T09:00:00+00:00", "changed_by": "local",
                    "source": "dashboard"},
        "performer": {"id": "perf-1", "backend": "claude-code", "transport": "http",
                      "endpoint": "endpoint-1"},
        "model": {"model": "claude-test-model"},
        "card": {"id": CARD, "issue_number": 133, "title": "Transcript viewer"},
    }


def _records() -> list[dict[str, object]]:
    return [
        {"ts": "2026-10-01T10:00:01+00:00", "run_id": RUN, "seq": 1, "type": "user",
         "text": "Build the viewer."},
        {"ts": "2026-10-01T10:00:02+00:00", "run_id": RUN, "seq": 2, "type": "assistant_text",
         "text": "hello <script>alert(1)</script>\nsecond line"},
        {"ts": "2026-10-01T10:00:03+00:00", "run_id": RUN, "seq": 3, "type": "thinking",
         "text": "considering the layout"},
        {"ts": "2026-10-01T10:00:04+00:00", "run_id": RUN, "seq": 4, "type": "tool_call",
         "tool_name": "Bash", "call_id": "c1", "input": {"command": "pytest -q tests/"}},
        {"ts": "2026-10-01T10:00:06+00:00", "run_id": RUN, "seq": 5, "type": "tool_result",
         "call_id": "c1", "is_error": True, "output": {"exit_code": 1, "stdout": "1 failed"}},
        {"ts": "2026-10-01T10:00:07+00:00", "run_id": RUN, "seq": 6, "type": "usage",
         "scope": "message", "usage": {"input_tokens": 100, "output_tokens": 20,
                                       "cache_read": 5, "cache_write": 2, "model": "claude-test-model"}},
        {"ts": "2026-10-01T10:00:08+00:00", "run_id": RUN, "seq": 7, "type": "usage",
         "scope": "message", "usage": {"input_tokens": 50, "output_tokens": 10}},
        {"ts": "2026-10-01T10:00:09+00:00", "run_id": RUN, "seq": 8, "type": "result",
         "is_error": False, "text": "Opened https://github.com/vividynamics/coordinare/pull/9"},
    ]


def _lines(records: list[dict[str, object]]) -> list[str]:
    return [json.dumps(r) for r in records]


def _write_run(root: Path, *, run: str = RUN, records: list[dict[str, object]] | None = None,
               header: dict[str, object] | None = None, extra: list[str] | None = None) -> Path:
    """A run file in the writer's layout: header first, then records, optionally a torn line."""
    path = transcript_path(root, symphony=SYMPHONY, card_id=CARD, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [json.dumps(header if header is not None else _header())]
    body += _lines(records if records is not None else _records())
    body += extra or []
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _client(root: Path | None, *, token: str | None = None) -> TestClient:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    if root is not None:
        daemon.state["transcript_collector"] = TranscriptCollector(
            root, enabled=True, max_bytes_per_run=10**9, retention_days=0,
        )
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        auth_token=SecretStr(token) if token is not None else None,
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    return TestClient(app, base_url=BASE)


def _run_url(run: str = RUN, card: str = CARD, symphony: str = SYMPHONY) -> str:
    return f"/api/runs/{symphony}/{card}/{run}"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "transcripts"
    base.mkdir()
    return base.resolve()


# --- auth --------------------------------------------------------------------- #


def test_every_run_route_requires_the_dashboard_token(root: Path) -> None:
    _write_run(root)
    client = _client(root, token=TOKEN)
    assert client.get(_run_url()).status_code == 401
    assert client.get(f"/api/card-runs/{CARD}").status_code == 401
    assert client.get(f"{_run_url()}/records").status_code == 401
    assert client.get(f"{_run_url()}/download.jsonl").status_code == 401
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get(_run_url(), headers=bearer).status_code == 200


def test_the_viewer_routes_are_declared_as_deferred_from_the_agent_surface() -> None:
    for path in (f"/api/runs/{SYMPHONY}/{CARD}/{RUN}", f"/api/card-runs/{CARD}", "/api/run-lookup/x",
                 "/runs/{run}", "/runs/{symphony}/{card}/{run}"):
        assert is_deferred(path), path


# --- listing ------------------------------------------------------------------ #


def test_a_card_lists_its_runs_newest_first_with_the_stage_from_the_header(root: Path) -> None:
    first = _write_run(root, run="run-old")
    os.utime(first, (1_000_000_000, 1_000_000_000))
    _write_run(root, run=RUN)
    body = _client(root).get(f"/api/runs/{SYMPHONY}/{CARD}").json()
    assert [r["run"] for r in body["runs"]] == [RUN, "run-old"]
    assert body["runs"][0]["stage"] == "implementing"
    assert body["runs"][0]["agent"] == "claude-code"
    assert body["runs"][0]["model"] == "claude-test-model"
    assert body["truncated"] is False


def test_the_card_listing_spans_symphonies_and_names_each_one(root: Path) -> None:
    _write_run(root)
    other = transcript_path(root, symphony="other-sym", card_id=CARD, run_id="run-other")
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps(_header()) + "\n", encoding="utf-8")
    body = _client(root).get(f"/api/card-runs/{CARD}").json()
    assert {(r["symphony"], r["run"]) for r in body["runs"]} == {(SYMPHONY, RUN), ("other-sym", "run-other")}


def test_a_run_with_no_header_is_listed_with_its_reason(root: Path) -> None:
    _write_run(root, header={"type": "user", "seq": 1, "text": "no header here"})
    row = _client(root).get(f"/api/runs/{SYMPHONY}/{CARD}").json()["runs"][0]
    assert row["available"] is False
    assert "run header" in row["reason"]


def test_listing_without_a_collector_says_transcripts_are_not_configured() -> None:
    body = _client(None).get(f"/api/card-runs/{CARD}").json()
    assert body["available"] is False
    assert body["runs"] == []


def test_run_lookup_resolves_a_run_id_to_its_address(root: Path) -> None:
    _write_run(root)
    body = _client(root).get(f"/api/run-lookup/{RUN}").json()
    assert body == {"symphony": SYMPHONY, "card": CARD, "run": RUN}
    assert _client(root).get("/api/run-lookup/run-nope").status_code == 404


# --- detail ------------------------------------------------------------------- #


def test_the_detail_carries_the_header_and_the_summary(root: Path) -> None:
    _write_run(root)
    body = _client(root).get(_run_url()).json()
    header = body["header_summary"]
    assert header["stage"] == "implementing"
    assert header["persona"]["version"] == 3
    assert header["endpoint"] == "endpoint-1"
    assert header["card"]["issue_number"] == 133
    summary = body["summary"]
    assert summary["records"] == len(_records())  # the header is not an event
    assert summary["errors"] == 1
    assert summary["tokens"]["scope"] == "message"
    assert summary["tokens"]["input"] == 150
    assert summary["tokens"]["output"] == 30
    assert summary["tokens"]["cache_read"] == 5
    assert summary["cost"]["value"] is None
    assert "#134" in summary["cost"]["reason"]
    assert summary["outcome"]["status"] == "completed"
    assert summary["pr_url"] == "https://github.com/vividynamics/coordinare/pull/9"
    assert summary["duration_seconds"] == 9.0


def test_run_scope_usage_wins_over_the_sum_of_the_turns(root: Path) -> None:
    records = [*_records(), {"seq": 9, "type": "usage", "scope": "run",
                             "usage": {"input_tokens": 999, "output_tokens": 1}}]
    _write_run(root, records=records)
    tokens = _client(root).get(_run_url()).json()["summary"]["tokens"]
    assert tokens["scope"] == "run"
    assert tokens["input"] == 999


def test_a_run_with_no_result_says_why_there_is_no_outcome(root: Path) -> None:
    _write_run(root, records=_records()[:3])
    outcome = _client(root).get(_run_url()).json()["summary"]["outcome"]
    assert outcome["status"] is None
    assert "no result record" in outcome["reason"]


def test_a_missing_run_is_404(root: Path) -> None:
    _write_run(root)
    assert _client(root).get(_run_url(run="run-missing")).status_code == 404


def test_a_run_address_the_writer_would_not_produce_is_404_before_any_disk_read(root: Path) -> None:
    client = _client(root)
    assert client.get(f"/api/runs/{SYMPHONY}/{CARD}/..").status_code == 404
    assert client.get(f"/api/runs/..%2Fetc/{CARD}/{RUN}").status_code == 404
    assert client.get("/api/runs/bad%20symphony/" + CARD + "/" + RUN).status_code == 404


# --- paging ------------------------------------------------------------------- #


def _seqs(records: list[dict[str, object]]) -> list[object]:
    return [r.get("seq") if not r.get("unreadable") else f"line{r['line']}" for r in records]


def test_pages_walk_the_whole_run_without_loss_or_repeat(root: Path) -> None:
    _write_run(root)
    client = _client(root)
    seen: list[object] = []
    after = 0
    for _ in range(20):
        page = client.get(f"{_run_url()}/records", params={"after": after, "limit": SMALL_PAGE}).json()
        seen.extend(_seqs(page["records"]))
        if page["next_after"] is None:
            break
        after = page["next_after"]
    assert seen == [1, 2, 3, 4, 5, 6, 7, 8]


def test_a_page_reports_the_line_it_continues_from(root: Path) -> None:
    _write_run(root)
    page = _client(root).get(f"{_run_url()}/records", params={"after": 0, "limit": SMALL_PAGE}).json()
    assert [r["line"] for r in page["records"]] == [2, 3]
    assert page["next_after"] == 3


def test_a_type_filter_returns_only_that_type(root: Path) -> None:
    _write_run(root)
    page = _client(root).get(f"{_run_url()}/records", params={"types": "tool_call,tool_result"}).json()
    assert [r["type"] for r in page["records"]] == ["tool_call", "tool_result"]


def test_the_errors_filter_returns_failed_results_and_unreadable_lines(root: Path) -> None:
    _write_run(root, extra=["{not json"])
    page = _client(root).get(f"{_run_url()}/records", params={"errors": "true"}).json()
    kinds = [(r["type"], r.get("unreadable", False)) for r in page["records"]]
    assert ("tool_result", False) in kinds
    assert ("unreadable", True) in kinds


def test_seq_positions_the_page_on_that_record(root: Path) -> None:
    _write_run(root)
    page = _client(root).get(f"{_run_url()}/records", params={"seq": 4}).json()
    assert page["focus_line"] == 5
    assert page["records"][0]["seq"] == 4


def test_at_positions_the_page_on_the_first_event_at_or_after_a_time(root: Path) -> None:
    _write_run(root)
    page = _client(root).get(
        f"{_run_url()}/records", params={"at": "2026-10-01T10:00:05+00:00"},
    ).json()
    assert page["records"][0]["seq"] == 5


def test_an_unparseable_at_is_a_bad_request(root: Path) -> None:
    _write_run(root)
    assert _client(root).get(f"{_run_url()}/records", params={"at": "yesterday"}).status_code == 400


def test_the_page_size_is_bounded(root: Path) -> None:
    _write_run(root)
    assert _client(root).get(f"{_run_url()}/records", params={"limit": 10_000}).status_code == 422


# --- unparsable lines --------------------------------------------------------- #


def test_an_unparseable_line_is_shown_with_its_line_number_and_never_dropped(root: Path) -> None:
    _write_run(root, extra=["this is not json", json.dumps({"seq": 99, "type": "result", "is_error": False,
                                                            "text": "after the tear"})])
    client = _client(root)
    page = client.get(f"{_run_url()}/records", params={"after": 0, "limit": 500}).json()
    unreadable = [r for r in page["records"] if r.get("unreadable")]
    assert len(unreadable) == 1
    assert unreadable[0]["line"] == 10
    assert unreadable[0]["preview"] == "this is not json"
    assert any(r.get("seq") == 99 for r in page["records"])
    assert client.get(_run_url()).json()["summary"]["unreadable"] == 1


# --- previews and full fields ------------------------------------------------- #


def test_a_long_output_is_previewed_and_flagged_and_its_full_value_is_one_request_away(root: Path) -> None:
    big = "x" * BIG_CHARS
    records = [{"seq": 1, "type": "tool_result", "call_id": "c9", "output": big}]
    _write_run(root, records=records)
    client = _client(root)
    record = client.get(f"{_run_url()}/records").json()["records"][0]
    assert record["output_truncated"] is True
    assert len(record["output"]) == run_viewer.PREVIEW_CHARS
    assert record["output_bytes"] == BIG_CHARS
    full = client.get(f"{_run_url()}/records/{record['line']}/output").json()
    assert full["value"] == big
    assert full["format"] == "text"
    assert full["bytes"] == BIG_CHARS


def test_a_structured_field_comes_back_in_full_as_json_text(root: Path) -> None:
    _write_run(root)
    full = _client(root).get(f"{_run_url()}/records/6/output").json()
    assert full["format"] == "json"
    assert json.loads(full["value"]) == {"exit_code": 1, "stdout": "1 failed"}


def test_a_field_the_viewer_does_not_load_in_full_is_refused(root: Path) -> None:
    _write_run(root)
    assert _client(root).get(f"{_run_url()}/records/5/bogus").status_code == 404


def test_a_line_beyond_the_file_is_404(root: Path) -> None:
    _write_run(root)
    assert _client(root).get(f"{_run_url()}/records/500/text").status_code == 404


# --- search ------------------------------------------------------------------- #


def test_search_finds_text_past_the_preview_because_it_searches_the_full_record(root: Path) -> None:
    needle = "needle-at-the-end"
    records = [{"seq": 1, "type": "assistant_text", "text": "a" * BIG_CHARS + needle}]
    _write_run(root, records=records)
    matches = _client(root).get(f"{_run_url()}/search", params={"q": needle}).json()["matches"]
    assert [m["seq"] for m in matches] == [1]


def test_search_is_case_insensitive_and_reports_the_seq_and_type(root: Path) -> None:
    _write_run(root)
    matches = _client(root).get(f"{_run_url()}/search", params={"q": "PYTEST"}).json()["matches"]
    assert [(m["seq"], m["type"]) for m in matches] == [(4, "tool_call")]


def test_search_with_the_errors_flag_finds_the_failed_result(root: Path) -> None:
    _write_run(root)
    matches = _client(root).get(f"{_run_url()}/search", params={"errors": "true"}).json()["matches"]
    assert [m["seq"] for m in matches] == [5]


# --- traversal and link refusal ----------------------------------------------- #


def test_a_symlinked_run_file_is_refused_with_403(root: Path, tmp_path: Path) -> None:
    real = _write_run(root)
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    real.unlink()
    os.symlink(elsewhere, real)
    assert _client(root).get(_run_url()).status_code == 403


def test_a_symlinked_card_folder_is_refused_with_403(root: Path, tmp_path: Path) -> None:
    _write_run(root)
    outside = tmp_path / "outside-card"
    outside.mkdir()
    (outside / f"{RUN}.jsonl").write_text(json.dumps(_header()) + "\n", encoding="utf-8")
    link = root / SYMPHONY / "PVTI_LINKED"
    os.symlink(outside, link)
    assert _client(root).get(_run_url(card="PVTI_LINKED")).status_code == 403


def test_a_symlinked_symphony_folder_outside_the_root_is_refused_with_403(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-sym"
    (outside / CARD).mkdir(parents=True)
    (outside / CARD / f"{RUN}.jsonl").write_text(json.dumps(_header()) + "\n", encoding="utf-8")
    os.symlink(outside, root / "linked-sym")
    assert _client(root).get(_run_url(symphony="linked-sym")).status_code == 403


def test_a_hard_linked_run_file_is_refused_with_403(root: Path, tmp_path: Path) -> None:
    real = _write_run(root)
    os.link(real, tmp_path / "second-name.jsonl")
    assert _client(root).get(_run_url()).status_code == 403


def test_a_symlink_in_the_listing_is_not_listed(root: Path, tmp_path: Path) -> None:
    _write_run(root)
    target = tmp_path / "planted.jsonl"
    target.write_text(json.dumps(_header()) + "\n", encoding="utf-8")
    os.symlink(target, root / SYMPHONY / CARD / "run-planted.jsonl")
    runs = [r["run"] for r in _client(root).get(f"/api/runs/{SYMPHONY}/{CARD}").json()["runs"]]
    assert runs == [RUN]


def test_the_helper_refuses_a_run_name_that_the_writer_would_sanitize(root: Path) -> None:
    with pytest.raises(run_viewer.RunLookupError):
        run_viewer.run_file(root, run_viewer.RunRef(SYMPHONY, CARD, "../escape"))


# --- downloads ---------------------------------------------------------------- #


def test_the_jsonl_download_is_the_stored_file_byte_for_byte(root: Path) -> None:
    path = _write_run(root)
    response = _client(root).get(f"{_run_url()}/download.jsonl")
    assert response.status_code == 200
    assert response.content == path.read_bytes()
    assert f'filename="{RUN}.jsonl"' in response.headers["content-disposition"]


def test_the_jsonl_download_streams_a_file_larger_than_one_chunk(root: Path) -> None:
    padding = "p" * (run_viewer.DOWNLOAD_CHUNK_BYTES * 3)
    records = [{"seq": 1, "type": "user", "text": padding}]
    path = _write_run(root, records=records)
    chunks = list(run_viewer.iter_bytes(run_viewer.open_run(path, run_viewer.RunRef(SYMPHONY, CARD, RUN))))
    assert len(chunks) > 1
    assert b"".join(chunks) == path.read_bytes()


def test_the_markdown_download_fences_every_value_so_nothing_is_markup(root: Path) -> None:
    _write_run(root)
    response = _client(root).get(f"{_run_url()}/download.md")
    assert response.status_code == 200
    body = response.text
    assert response.headers["content-type"].startswith("text/markdown")
    assert "# Run run-133" in body
    assert "- Stage: implementing" in body
    assert "<script>alert(1)</script>" in body
    assert "pytest -q tests/" in body
    assert "### 5 · tool_result" in body


def test_the_markdown_fence_grows_past_any_backtick_run_in_the_content(root: Path) -> None:
    records = [{"seq": 1, "type": "assistant_text", "text": "code: ``` and ````` inside"}]
    _write_run(root, records=records)
    body = _client(root).get(f"{_run_url()}/download.md").text
    assert "``````text\ncode: ``` and ````` inside\n``````" in body


def test_the_markdown_download_keeps_an_unreadable_line(root: Path) -> None:
    _write_run(root, extra=["torn line"])
    body = _client(root).get(f"{_run_url()}/download.md").text
    assert "### Line 10: unreadable" in body
    assert "torn line" in body


def test_a_missing_run_download_is_404(root: Path) -> None:
    _write_run(root)
    assert _client(root).get(f"{_run_url(run='nope')}/download.jsonl").status_code == 404
    assert _client(root).get(f"{_run_url(run='nope')}/download.md").status_code == 404


# --- the page -------------------------------------------------------------------- #


def test_the_run_pages_serve_the_dashboard_shell(root: Path) -> None:
    client = _client(root)
    shell = client.get("/").text
    assert client.get(f"/runs/{SYMPHONY}/{CARD}/{RUN}").text == shell
    assert client.get(f"/runs/{RUN}").text == shell


def test_other_run_ids_still_resolve_only_to_the_run_that_exists(root: Path) -> None:
    _write_run(root)
    _write_run(root, run=OTHER_RUN)
    body = _client(root).get(f"/api/run-lookup/{OTHER_RUN}").json()
    assert body["run"] == OTHER_RUN

"""#150 review round: capped scans, cursor validation, anchors, budgets, ledger bounds.

The listing is services/run_history.py. Each test here was written to fail on the code the
review examined, before the fix.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.localhost_guard import build_permitted
from coordinare.services import run_history, token_usage
from coordinare.services.run_history import (
    HistoryFilter,
    HistoryQuery,
    HistoryQueryError,
    UsageSource,
    list_history,
)
from coordinare.services.run_transcript import TranscriptCollector, transcript_path
from coordinare.services.token_usage import TokenLedger, price_table_from_config

BASE = "http://127.0.0.1:8090"
TOKEN = "a-secret-operator-token-of-at-least-32-characters"
SYMPHONY = "coordinare"
CARD_A = "PVTI_A"
CARD_B = "PVTI_B"
MODEL = "claude-test-model"
EPOCH = 1_700_000_000
BASE_TS = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def _write(root: Path, run: str, card: str, *, mtime: int, symphony: str = SYMPHONY) -> Path:
    path = transcript_path(root, symphony=symphony, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "run_header", "seq": 0, "header_version": 1, "ts": BASE_TS.isoformat(), "run_id": run,
              "persona": {"name": "implementing", "version": 1, "version_sha256": "ab" * 32},
              "performer": {"id": "p", "backend": "claude-code", "transport": "http", "endpoint": "e"},
              "model": {"model": MODEL}, "card": {"id": card, "issue_number": 1, "title": "t"}}
    result = {"type": "result", "ts": (BASE_TS + timedelta(minutes=1)).isoformat(), "run_id": run, "seq": 1,
              "is_error": False, "text": "done"}
    path.write_text("\n".join(json.dumps(line) for line in (header, result)) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _set_folder_mtime(root: Path, card: str, seconds: int) -> None:
    os.utime(root / SYMPHONY / card, (seconds, seconds))


def _usage(root: Path) -> UsageSource:
    ledger = TokenLedger()
    ledger.load_from_transcripts(root, now=BASE_TS + timedelta(days=1))
    return UsageSource(ledger=ledger, table=price_table_from_config(None))


def _query(**kwargs: Any) -> HistoryQuery:
    limit = kwargs.pop("limit", 50)
    cursor = kwargs.pop("cursor", None)
    visible = kwargs.pop("visible", frozenset({SYMPHONY}))
    return HistoryQuery(filters=HistoryFilter(**kwargs), visible=visible, limit=limit, cursor=cursor)


def _runs(body: dict[str, Any]) -> list[str]:
    return [row["run"] for row in body["rows"]]


def _client(root: Path) -> TestClient:
    daemon = MagicMock()
    daemon.state = {"startup_complete": True, "phase": "idle", "error_count": 0, "symphony_configs": {SYMPHONY: object()}, "config": None}
    daemon.state["transcript_collector"] = TranscriptCollector(
        root, enabled=True, max_bytes_per_run=10**9, retention_days=0,
    )
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        auth_token=SecretStr(TOKEN),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    return TestClient(app, base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"})


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "transcripts"
    base.mkdir()
    return base.resolve()


@pytest.fixture
def capped(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Ten old runs in one card folder and the newest run in another, under a 4-entry scan budget."""
    # Symphony and conductor checks count too, so eight entries leave both card folders room.
    monkeypatch.setattr(run_history, "HISTORY_STAT_MAX", 8)
    for index in range(10):
        _write(root, f"old{index}", CARD_A, mtime=EPOCH + index)
    _write(root, "newest", CARD_B, mtime=EPOCH + 10_000)
    _set_folder_mtime(root, CARD_A, EPOCH)
    _set_folder_mtime(root, CARD_B, EPOCH + 9_000)
    return root


# --- block 1: a capped walk --------------------------------------------------- #


def test_a_capped_walk_still_reaches_the_newest_run(capped: Path) -> None:
    body = list_history(capped, _query(), _usage(capped))
    assert "newest" in _runs(body)


def test_a_capped_listing_says_it_is_incomplete(capped: Path) -> None:
    body = list_history(capped, _query(), _usage(capped))
    assert body["partial"] is True
    assert run_history.STAT_CAP_REASON in body["partial_reasons"]
    assert any("incomplete" in detail for detail in body["partial_details"])


def test_the_scan_never_stats_more_than_its_budget(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history, "HISTORY_STAT_MAX", 10)
    for card_index in range(200):
        for run_index in range(3):
            _write(root, f"r{run_index}", f"PVTI_{card_index:03d}", mtime=EPOCH + run_index)
    body = list_history(root, _query(), _usage(root))
    assert body["scanned"] <= 10
    assert body["partial"] is True


def test_a_card_folder_past_the_per_card_read_limit_is_reported(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history, "HISTORY_CARD_FILES_MAX", 2)
    for index in range(5):
        _write(root, f"many{index}", CARD_A, mtime=EPOCH + index)
    body = list_history(root, _query(), _usage(root))
    assert body["partial"] is True
    assert run_history.CARD_CAP_REASON in body["partial_reasons"]


def test_entries_that_are_not_transcripts_are_not_statted(root: Path) -> None:
    _write(root, "run-real", CARD_A, mtime=EPOCH)
    folder = root / SYMPHONY / CARD_A
    for index in range(50):
        (folder / f"note{index}.txt").write_text("not a run", encoding="utf-8")
    body = list_history(root, _query(), _usage(root))
    assert _runs(body) == ["run-real"]
    assert body["scanned"] <= 4  # the symphony and conductor checks, the card folder, and the one transcript


# --- block 2: cursors ---------------------------------------------------------- #

# The probe's crafted values (four parts reach the number parse on the old format), and the
# same faults in the anchor and mtime positions of the current five-part format.
BAD_CURSORS = [
    "²|alpha|c1|r1", "1" * 5000 + "|alpha|c1|r1", "1|alpha|c1|r1",
    "²|1|alpha|c1|r1", "1|²|alpha|c1|r1", "1" * 5000 + "|1|alpha|c1|r1", "1|" + "1" * 5000 + "|alpha|c1|r1",
]


@pytest.mark.parametrize("cursor", BAD_CURSORS)
def test_a_malformed_cursor_is_refused_as_a_query_error(root: Path, cursor: str) -> None:
    _write(root, "run-1", CARD_A, mtime=EPOCH)
    with pytest.raises(HistoryQueryError):
        list_history(root, _query(cursor=cursor), _usage(root))


@pytest.mark.parametrize("cursor", BAD_CURSORS)
def test_a_malformed_cursor_is_a_400_through_the_route(root: Path, cursor: str) -> None:
    _write(root, "run-1", CARD_A, mtime=EPOCH)
    assert _client(root).get("/api/history/runs", params={"cursor": cursor}).status_code == 400


# --- should-fix: card filter, examine cap ------------------------------------------ #


@pytest.mark.parametrize("card", ["a/b", "..", "x y"])
def test_a_card_filter_that_is_not_a_path_segment_is_refused(root: Path, card: str) -> None:
    _write(root, "run-1", CARD_A, mtime=EPOCH)
    with pytest.raises(HistoryQueryError):
        list_history(root, _query(card=card), _usage(root))
    assert _client(root).get("/api/history/runs", params={"card": card}).status_code == 400


def test_a_page_cut_short_by_the_examine_cap_says_why(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history, "HISTORY_EXAMINE_MAX", 2)
    for index in range(5):
        _write(root, f"ok{index}", CARD_A, mtime=EPOCH + index)
    body = list_history(root, _query(outcome="failed"), _usage(root))
    assert body["rows"] == []
    assert body["next_cursor"] is not None
    assert body["partial"] is True
    assert run_history.EXAMINE_CAP_REASON in body["partial_reasons"]
    assert body["partial_details"]


# --- block 3: the anchor ------------------------------------------------------- #


def test_a_run_written_between_pages_is_neither_skipped_nor_repeated(root: Path) -> None:
    for index in range(7):
        _write(root, f"base{index}", CARD_A, mtime=EPOCH + index)
    first = list_history(root, _query(limit=3), _usage(root))
    second = list_history(root, _query(limit=3, cursor=first["next_cursor"]), _usage(root))
    _write(root, "fresh", CARD_A, mtime=time.time_ns())  # written now, so newer than page one's anchor
    third = list_history(root, _query(limit=3, cursor=second["next_cursor"]), _usage(root))
    listed = _runs(first) + _runs(second) + _runs(third)
    assert "fresh" not in listed
    assert len(listed) == len(set(listed))
    assert third["newer_since_anchor"] == 1
    assert first["newer_since_anchor"] == 0


# --- block 4 and should-fix: ledger lookups and bounds --------------------------- #


def test_the_ledger_answers_a_page_in_one_pass() -> None:
    ledger = TokenLedger()
    for run in ("run-000", "run-001"):
        ledger.observe(
            run, symphony=SYMPHONY, card=CARD_A, stage="implementing",
            records=[{"seq": 1, "ts": BASE_TS.isoformat(), "type": "usage", "scope": "message",
                      "usage": {"input_tokens": 3, "output_tokens": 4, "model": MODEL}}],
        )
    by_run = ledger.totals_by_run(["run-000", "run-001", "run-missing"])
    assert set(by_run) == {"run-000", "run-001"}
    assert by_run["run-000"][MODEL].total == 7


def test_the_ledger_totals_cache_is_bounded() -> None:
    ledger = TokenLedger()
    for index in range(token_usage.TOTALS_CACHE_MAX + 50):
        ledger.observe(
            f"r{index}", symphony=SYMPHONY, card=CARD_A, stage="implementing",
            records=[{"seq": 1, "ts": BASE_TS.isoformat(), "type": "usage", "scope": "message",
                      "usage": {"input_tokens": 1, "output_tokens": 1, "model": MODEL}}],
        )
        ledger.totals(run_id=f"r{index}")
    assert len(ledger._cache) <= token_usage.TOTALS_CACHE_MAX



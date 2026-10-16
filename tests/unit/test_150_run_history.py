"""#150: the History page's run listing, read from the transcripts and the token ledger.

The listing is services/run_history.py and the route is dashboard/routers/history.py. The
browser behaviour is in tests/e2e/test_150_history.py and the client helpers in
tests/unit/test_150_history_js.py.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import CostTrackingConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.lib.path_segments import CONDUCTOR_FOLDER
from coordinare.localhost_guard import build_permitted
from coordinare.mcp.tools import is_deferred
from coordinare.services import run_history
from coordinare.services.run_history import (
    HISTORY_DEFAULT_LIMIT,
    HistoryFilter,
    HistoryQuery,
    HistoryQueryError,
    HistorySymphonyError,
    UsageSource,
    list_history,
)
from coordinare.services.run_transcript import TranscriptCollector, transcript_path
from coordinare.services.token_usage import TokenLedger, price_table_from_config

BASE = "http://127.0.0.1:8090"
TOKEN = "a-secret-operator-token-of-at-least-32-characters"
SYMPHONY = "coordinare"
OTHER = "hidden-sym"
VISIBLE = frozenset({SYMPHONY, "beta-sym"})
CARD_A = "PVTI_A"
CARD_B = "PVTI_B"
BASE_TS = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
SECRET_TITLE = "fix the ghp_" + "a" * 36 + " leak"
PRICES = CostTrackingConfig.model_validate({
    "pricing_as_of": "2026-10-01",
    "prices": [{
        "models": ["claude-test-model"],
        "input_per_million": 10.0,
        "output_per_million": 50.0,
        "cache_read_per_million": 1.0,
        "cache_write_per_million": 12.5,
    }],
})
NO_PRICES = CostTrackingConfig.model_validate({})


def _at(minutes: float) -> str:
    return (BASE_TS + timedelta(minutes=minutes)).isoformat()


def _header(run: str, card: str, *, stage: str = "implementing", minutes: float = 0,
            title: str = "Make history useful", model: str = "claude-test-model") -> dict[str, Any]:
    return {
        "type": "run_header", "seq": 0, "header_version": 1, "ts": _at(minutes), "run_id": run,
        "persona": {"name": stage, "version": 1, "version_sha256": "ab" * 32},
        "performer": {"id": "perf-1", "backend": "claude-code", "transport": "http", "endpoint": "ep-1"},
        "model": {"model": model},
        "card": {"id": card, "issue_number": 150, "title": title},
    }


def _records(run: str, *, minutes: float, pr: bool = False, failed: bool = False,
             model: str = "claude-test-model", usage: bool = True) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = [
        {"ts": _at(minutes + 0.1), "run_id": run, "seq": 1, "type": "user", "text": "work"},
    ]
    if usage:
        records.append({"ts": _at(minutes + 0.2), "run_id": run, "seq": 2, "type": "usage",
                        "scope": "message",
                        "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read": 0,
                                  "cache_write": 0, "model": model}})
    text = "Opened https://github.com/vividynamics/coordinare/pull/150" if pr else "Stopped."
    records.append({"ts": _at(minutes + 1), "run_id": run, "seq": 3, "type": "result",
                    "is_error": failed, "text": text})
    return records


def _write(root: Path, run: str, card: str, *, symphony: str = SYMPHONY, header: dict[str, Any] | None = None,
           records: list[dict[str, Any]] | None = None, mtime: float | None = None) -> Path:
    # A conductor call lives in the conductor's own folder, which the writer does not sanitise.
    if symphony == CONDUCTOR_FOLDER:
        path = root / CONDUCTOR_FOLDER / card / f"{run}.jsonl"
    else:
        path = transcript_path(root, symphony=symphony, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [json.dumps(header if header is not None else _header(run, card))]
    body += [json.dumps(r) for r in (records if records is not None else _records(run, minutes=0))]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "transcripts"
    base.mkdir()
    return base.resolve()


def _ledger_for(root: Path) -> TokenLedger:
    """The ledger rebuilt from the files, as a daemon does at boot."""
    ledger = TokenLedger()
    ledger.load_from_transcripts(root, now=BASE_TS + timedelta(days=1))
    return ledger


def _usage(*, prices: CostTrackingConfig = PRICES, root: Path | None = None) -> UsageSource:
    ledger = _ledger_for(root) if root is not None else TokenLedger()
    return UsageSource(ledger=ledger, table=price_table_from_config(prices))


def _query(**filters: Any) -> HistoryQuery:
    limit = filters.pop("limit", HISTORY_DEFAULT_LIMIT)
    cursor = filters.pop("cursor", None)
    visible = filters.pop("visible", VISIBLE)
    return HistoryQuery(filters=HistoryFilter(**filters), visible=visible, limit=limit, cursor=cursor)


def _runs(body: dict[str, Any]) -> list[str]:
    return [row["run"] for row in body["rows"]]


def _seed(root: Path, count: int, *, card: str = CARD_A) -> list[str]:
    """``count`` runs of one card, the first oldest. Returns their names, oldest first."""
    names = []
    for index in range(count):
        run = f"run-{index:03d}"
        _write(root, run, card, records=_records(run, minutes=index * 10), mtime=1_700_000_000 + index * 100)
        names.append(run)
    return names


# --- pagination ----------------------------------------------------------------- #


def test_one_page_holds_every_run_when_the_limit_is_exact(root: Path) -> None:
    names = _seed(root, 3)
    body = list_history(root, _query(limit=3), _usage(root=root))
    assert _runs(body) == list(reversed(names))
    assert body["next_cursor"] is None


def test_the_next_cursor_appears_one_run_past_the_limit(root: Path) -> None:
    names = _seed(root, 4)
    body = list_history(root, _query(limit=3), _usage(root=root))
    assert len(body["rows"]) == 3
    assert body["next_cursor"] is not None
    assert names[0] not in _runs(body)


def test_walking_every_page_yields_each_run_once_in_order(root: Path) -> None:
    names = _seed(root, 7)
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        body = list_history(root, _query(limit=2, cursor=cursor), _usage(root=root))
        seen.extend(_runs(body))
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == list(reversed(names))
    assert len(seen) == len(set(seen))


def test_a_malformed_cursor_is_refused(root: Path) -> None:
    _seed(root, 1)
    with pytest.raises(HistoryQueryError):
        list_history(root, _query(cursor="not-a-cursor"), _usage(root=root))
    with pytest.raises(HistoryQueryError):
        list_history(root, _query(cursor="abc|x|y|z"), _usage(root=root))


def test_the_examine_bound_stops_a_page_and_leaves_a_cursor(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(root, 5)
    monkeypatch.setattr(run_history, "HISTORY_EXAMINE_MAX", 2)
    body = list_history(root, _query(stage="no-such-stage", limit=10), _usage(root=root))
    assert body["rows"] == []
    assert body["examined"] == 2
    assert body["next_cursor"] is not None


def test_the_scan_bound_is_reported_as_partial(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(root, 6)
    monkeypatch.setattr(run_history, "HISTORY_STAT_MAX", 4)
    body = list_history(root, _query(limit=50), _usage(root=root))
    assert body["partial"] is True
    # Symphony and card folder checks count against the budget, so a cap of four reads few runs.
    assert body["scanned"] <= 4
    assert run_history.STAT_CAP_REASON in body["partial_reasons"] or (
        run_history.FOLDER_CAP_REASON in body["partial_reasons"]
    )


def test_an_unbounded_listing_is_not_partial(root: Path) -> None:
    _seed(root, 6)
    body = list_history(root, _query(limit=50), _usage(root=root))
    assert body["partial"] is False
    # Two visible symphonies and the conductor folder, the card folder, and six transcripts.
    assert body["scanned"] == 10


# --- filters ------------------------------------------------------------------ #


def test_the_symphony_filter_keeps_one_symphony(root: Path) -> None:
    _write(root, "run-a", CARD_A, symphony=SYMPHONY, header=_header("run-a", CARD_A))
    _write(root, "run-b", CARD_A, symphony="beta-sym", header=_header("run-b", CARD_A))
    body = list_history(root, _query(symphony="beta-sym"), _usage(root=root))
    assert _runs(body) == ["run-b"]
    assert body["rows"][0]["symphony"] == "beta-sym"


def test_the_card_filter_keeps_one_card(root: Path) -> None:
    _write(root, "run-a", CARD_A)
    _write(root, "run-b", CARD_B)
    body = list_history(root, _query(card=CARD_B), _usage(root=root))
    assert _runs(body) == ["run-b"]


def test_the_stage_filter_matches_the_header_stage(root: Path) -> None:
    _write(root, "run-impl", CARD_A, header=_header("run-impl", CARD_A, stage="implementing"))
    _write(root, "run-qa", CARD_A, header=_header("run-qa", CARD_A, stage="qa"))
    body = list_history(root, _query(stage="qa"), _usage(root=root))
    assert _runs(body) == ["run-qa"]
    assert body["rows"][0]["stage"] == "qa"


def test_the_outcome_filter_separates_failed_completed_and_unknown(root: Path) -> None:
    _write(root, "run-ok", CARD_A, records=_records("run-ok", minutes=0))
    _write(root, "run-bad", CARD_A, records=_records("run-bad", minutes=0, failed=True))
    unfinished = _records("run-open", minutes=0)[:-1]
    _write(root, "run-open", CARD_A, records=unfinished)
    usage = _usage(root=root)
    assert _runs(list_history(root, _query(outcome="failed"), usage)) == ["run-bad"]
    assert _runs(list_history(root, _query(outcome="completed"), usage)) == ["run-ok"]
    unknown = list_history(root, _query(outcome="unknown"), usage)
    assert _runs(unknown) == ["run-open"]
    assert unknown["rows"][0]["outcome_reason"]


def test_the_date_range_is_inclusive_on_both_ends(root: Path) -> None:
    _write(root, "run-day1", CARD_A, header=_header("run-day1", CARD_A, minutes=0))
    _write(root, "run-day2", CARD_A, header=_header("run-day2", CARD_A, minutes=60 * 24))
    _write(root, "run-day3", CARD_A, header=_header("run-day3", CARD_A, minutes=60 * 48))
    usage = _usage(root=root)
    body = list_history(root, _query(since=date(2026, 10, 2), until=date(2026, 10, 2)), usage)
    assert _runs(body) == ["run-day2"]
    assert _runs(list_history(root, _query(since=date(2026, 10, 2)), usage)) == ["run-day3", "run-day2"]
    assert _runs(list_history(root, _query(until=date(2026, 10, 1)), usage)) == ["run-day1"]


def test_the_filters_combine(root: Path) -> None:
    _write(root, "run-1", CARD_A, header=_header("run-1", CARD_A, stage="qa"), records=_records("run-1", minutes=0, failed=True))
    _write(root, "run-2", CARD_B, header=_header("run-2", CARD_B, stage="qa"), records=_records("run-2", minutes=0))
    body = list_history(root, _query(card=CARD_A, stage="qa", outcome="failed"), _usage(root=root))
    assert _runs(body) == ["run-1"]


# --- durability --------------------------------------------------------------- #


def test_a_fresh_listing_after_a_restart_returns_the_same_rows(root: Path) -> None:
    _seed(root, 3)
    before = list_history(root, _query(limit=10), _usage(root=root))
    after = list_history(root, _query(limit=10), _usage(root=root))
    assert _runs(after) == _runs(before)
    assert after["rows"] == before["rows"]


# --- columns and their sources ---------------------------------------------------- #


def test_the_row_carries_each_column_from_its_source(root: Path) -> None:
    _write(root, "run-x", CARD_A, header=_header("run-x", CARD_A, minutes=0),
           records=_records("run-x", minutes=0, pr=True))
    row = list_history(root, _query(), _usage(root=root))["rows"][0]
    assert row["started_at"] == _at(0)
    assert row["stage"] == "implementing"
    assert row["agent"] == "claude-code"
    assert row["card"] == CARD_A
    assert row["outcome"] == "completed"
    assert row["pr_url"] == "https://github.com/vividynamics/coordinare/pull/150"
    assert row["duration_seconds"] == pytest.approx(60.0)


def test_a_run_without_a_header_shows_its_stage_as_not_recorded(root: Path) -> None:
    path = _write(root, "run-old", CARD_A, records=_records("run-old", minutes=0))
    path.write_text("\n".join(json.dumps(r) for r in _records("run-old", minutes=0)) + "\n", encoding="utf-8")
    row = list_history(root, _query(), _usage(root=root))["rows"][0]
    assert row["stage"] is None
    assert row["stage_reason"]


def test_the_pr_is_not_recorded_when_the_result_has_no_link(root: Path) -> None:
    _write(root, "run-np", CARD_A, records=_records("run-np", minutes=0))
    row = list_history(root, _query(), _usage(root=root))["rows"][0]
    assert row["pr_url"] is None
    assert row["pr_reason"]


# --- ledger cost and unknown cost ---------------------------------------------- #


def test_a_priced_run_has_its_ledger_tokens_and_cost(root: Path) -> None:
    _write(root, "run-priced", CARD_A, records=_records("run-priced", minutes=0))
    row = list_history(root, _query(), _usage(prices=PRICES, root=root))["rows"][0]
    assert row["tokens"] == 1200
    assert row["cost_usd"] == pytest.approx(10.0 * 1000 / 1e6 + 50.0 * 200 / 1e6)
    assert row["cost_reason"] is None


def test_an_unpriced_model_shows_tokens_and_an_unknown_cost(root: Path) -> None:
    _write(root, "run-unpriced", CARD_A, records=_records("run-unpriced", minutes=0))
    row = list_history(root, _query(), _usage(prices=NO_PRICES, root=root))["rows"][0]
    assert row["tokens"] == 1200
    assert row["cost_usd"] is None
    assert row["cost_reason"]


def test_a_run_with_no_usage_records_has_no_tokens_or_cost(root: Path) -> None:
    _write(root, "run-none", CARD_A, records=_records("run-none", minutes=0, usage=False))
    row = list_history(root, _query(), _usage(root=root))["rows"][0]
    assert row["tokens"] is None
    assert row["tokens_reason"] == run_history.NO_USAGE_REASON


def test_without_a_ledger_tokens_and_cost_are_not_tracked(root: Path) -> None:
    _write(root, "run-off", CARD_A)
    usage = UsageSource(ledger=None, table=price_table_from_config(PRICES))
    row = list_history(root, _query(), usage)["rows"][0]
    assert row["tokens"] is None
    assert row["cost_usd"] is None
    assert row["cost_reason"] == run_history.NO_LEDGER_REASON


# --- scope ----------------------------------------------------------------------- #


def test_a_symphony_the_daemon_does_not_run_is_refused(root: Path) -> None:
    _write(root, "run-h", CARD_A, symphony=OTHER)
    with pytest.raises(HistorySymphonyError):
        list_history(root, _query(symphony=OTHER), _usage(root=root))


def test_the_folders_of_an_unconfigured_symphony_are_not_listed(root: Path) -> None:
    _write(root, "run-v", CARD_A, symphony=SYMPHONY)
    _write(root, "run-h", CARD_A, symphony=OTHER)
    body = list_history(root, _query(), _usage(root=root))
    assert _runs(body) == ["run-v"]


def test_a_conductor_call_is_listed_only_for_a_symphony_the_viewer_sees(root: Path) -> None:
    conductor_visible = _header("run-cv", CARD_A, stage="conductor")
    conductor_visible["conductor"] = {"kind": "triage", "decision": "proceed", "symphony": SYMPHONY}
    conductor_hidden = _header("run-ch", CARD_A, stage="conductor")
    conductor_hidden["conductor"] = {"kind": "triage", "decision": "proceed", "symphony": OTHER}
    _write(root, "run-cv", CARD_A, symphony="_conductor", header=conductor_visible)
    _write(root, "run-ch", CARD_A, symphony="_conductor", header=conductor_hidden)
    body = list_history(root, _query(), _usage(root=root))
    assert _runs(body) == ["run-cv"]
    assert body["rows"][0]["kind"] == "check"
    assert body["rows"][0]["attributed_symphony"] == SYMPHONY


def test_the_scope_is_checked_before_any_file_is_read(root: Path) -> None:
    _write(root, "run-h", CARD_A, symphony=OTHER)
    with pytest.raises(HistorySymphonyError):
        list_history(root, _query(symphony=OTHER, visible=frozenset({SYMPHONY})), _usage(root=root))


# --- redaction ------------------------------------------------------------------- #


def test_free_text_from_a_header_is_redacted(root: Path) -> None:
    _write(root, "run-r", CARD_A, header=_header("run-r", CARD_A, title=SECRET_TITLE))
    row = list_history(root, _query(), _usage(root=root))["rows"][0]
    assert "ghp_" not in row["card_title"]
    assert "a" * 36 not in row["card_title"]


# --- the route ------------------------------------------------------------------- #


def _client(root: Path | None, *, token: str | None = None) -> TestClient:
    daemon = MagicMock()
    daemon.state = {"startup_complete": True, "phase": "idle", "error_count": 0, "symphony_configs": {SYMPHONY: object(), "beta-sym": object()},
                    "config": None}
    if root is not None:
        collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**9, retention_days=0)
        daemon.state["transcript_collector"] = collector
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        auth_token=SecretStr(token) if token is not None else None,
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    return TestClient(app, base_url=BASE)


def test_the_route_is_under_the_dashboard_token(root: Path) -> None:
    _seed(root, 1)
    client = _client(root, token=TOKEN)
    assert client.get("/api/history/runs").status_code == 401
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/history/runs", headers=bearer).status_code == 200


def test_the_route_returns_the_listing_and_the_scope_refusal(root: Path) -> None:
    _seed(root, 2)
    client = _client(root)
    body = client.get("/api/history/runs", params={"limit": 1}).json()
    assert body["available"] is True
    assert len(body["rows"]) == 1
    assert body["next_cursor"] is not None
    refused = client.get("/api/history/runs", params={"symphony": OTHER})
    assert refused.status_code == 404
    assert refused.headers["cache-control"] == "no-store"


def test_the_route_refuses_a_bad_day_and_a_bad_cursor(root: Path) -> None:
    _seed(root, 1)
    client = _client(root)
    assert client.get("/api/history/runs", params={"since": "2026-13-40"}).status_code == 400
    assert client.get("/api/history/runs", params={"cursor": "junk"}).status_code == 400
    assert client.get("/api/history/runs", params={"outcome": "maybe"}).status_code == 422
    assert client.get("/api/history/runs", params={"limit": 0}).status_code == 422


def test_the_route_says_so_when_transcripts_are_off() -> None:
    body = _client(None).get("/api/history/runs").json()
    assert body["available"] is False
    assert body["rows"] == []


def test_the_route_is_declared_as_deferred_from_the_agent_surface() -> None:
    assert is_deferred("/api/history/runs")

"""#147: the timeline endpoint and the board strip, read from a daemon's state.

Covers the route's answers (a card of its own symphony, a card of another symphony, an unknown
card), the symphony scope it enforces, the redaction of free text, the run list and durations
from the transcript index, the cost from the ledger, the strip the snapshot carries, and the
route's registration in the MCP deferred list.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard.card_pipeline import card_pipeline_block
from coordinare.localhost_guard import build_permitted
from coordinare.mcp.tools import is_deferred
from coordinare.services.card_lifecycle import CardLifecycleLog
from coordinare.services.run_transcript import TranscriptCollector, transcript_path

BASE = "http://127.0.0.1:8090"
TOKEN = "a-secret-operator-token-of-at-least-32-characters"
ALPHA = "alpha-sym"
BETA = "beta-sym"
CARD = "PVTI_A1"
BETA_CARD = "PVTI_B1"
RUN = "run147a1"
SECRET = "ghp_" + "B" * 36
T0 = "2026-10-14T09:00:00+00:00"
T1 = "2026-10-14T09:02:30+00:00"
LIFECYCLE = ["assessing", "implementing", "reviewing", "closing_review"]


def _header(run: str, card: str) -> dict[str, Any]:
    return {
        "type": "run_header", "seq": 0, "header_version": 1, "ts": T0, "run_id": run,
        "persona": {"name": "implementer", "version": 2, "version_sha256": "ab" * 32,
                    "changed_at": T0, "changed_by": "local", "source": "dashboard"},
        "performer": {"id": "perf-1", "backend": "claude-code", "transport": "http", "endpoint": "ep"},
        "model": {"model": "claude-test-model"},
        "card": {"id": card, "issue_number": 147, "title": "Timeline"},
    }


def _write_run(root: Path, symphony: str, card: str, run: str) -> None:
    path = transcript_path(root, symphony=symphony, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        _header(run, card),
        {"ts": T0, "seq": 1, "type": "user", "text": "Build it."},
        {"ts": T1, "seq": 2, "type": "result", "is_error": False, "text": "Done."},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _log() -> CardLifecycleLog:
    log = CardLifecycleLog()
    log.record(ALPHA, CARD, "dispatched", stage="implementing", at=T0)
    log.record(ALPHA, CARD, "pr_opened", stage="implementing", at=T0,
               detail={"pr_number": 9, "pr_url": "https://github.com/acme/repo/pull/9"}, key="pr")
    log.record(ALPHA, CARD, "human_action", stage="implementing", at=T1,
               detail={"action": f"skip {SECRET}"}, key="review:1")
    log.record(BETA, BETA_CARD, "dispatched", stage="reviewing", at=T0)
    return log


def _daemon(root: Path | None, log: CardLifecycleLog | None) -> Any:
    daemon = MagicMock()
    symphonies = {ALPHA: SimpleNamespace(), BETA: SimpleNamespace()}
    daemon.state = {
        "phase": "idle",
        "error_count": 0,
        "symphony_configs": symphonies,
        "symphony_states": {},
        "lifecycle_sequence": LIFECYCLE,
        "active_sessions": {},
        "card_lifecycle": log,
    }
    if root is not None:
        daemon.state["transcript_collector"] = TranscriptCollector(
            root, enabled=True, max_bytes_per_run=10**9, retention_days=0,
        )
    daemon.state_store.last_snapshot = None
    return daemon


def _client(daemon: Any) -> TestClient:
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        auth_token=SecretStr(TOKEN),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    return TestClient(app, base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"})


def _url(symphony: str, card: str) -> str:
    return f"/api/cards/{symphony}/{card}/timeline"


@pytest.fixture
def transcript_root(tmp_path: Path) -> Path:
    root = tmp_path / "transcripts"
    _write_run(root, ALPHA, CARD, RUN)
    return root


def test_a_card_of_its_symphony_gets_its_timeline(transcript_root: Path) -> None:
    client = _client(_daemon(transcript_root, _log()))
    response = client.get(_url(ALPHA, CARD))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["symphony"] == ALPHA
    assert body["card_id"] == CARD
    assert next(stage["stage"] for stage in body["stages"]) == "assessing"
    assert len(body["stages"]) == 8
    kinds = [event["kind"] for event in body["events"]]
    assert kinds == ["dispatched", "pr_opened", "human_action"]


def test_a_card_of_another_symphony_is_not_found_under_this_one(transcript_root: Path) -> None:
    client = _client(_daemon(transcript_root, _log()))
    response = client.get(_url(ALPHA, BETA_CARD))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert client.get(_url(BETA, BETA_CARD)).status_code == 200


def test_an_unknown_symphony_or_card_is_not_found(transcript_root: Path) -> None:
    client = _client(_daemon(transcript_root, _log()))
    assert client.get(_url("nowhere", CARD)).status_code == 404
    assert client.get(_url(ALPHA, "PVTI_NONE")).status_code == 404


def test_the_route_is_read_only(transcript_root: Path) -> None:
    client = _client(_daemon(transcript_root, _log()))
    assert client.post(_url(ALPHA, CARD), json={}).status_code in {404, 405}
    assert client.put(_url(ALPHA, CARD), json={}).status_code in {404, 405}


def test_free_text_leaves_the_server_redacted(transcript_root: Path) -> None:
    client = _client(_daemon(transcript_root, _log()))
    body = client.get(_url(ALPHA, CARD)).text
    assert SECRET not in body
    assert "skip" in body


def test_runs_come_from_the_transcript_index_with_their_recorded_duration(transcript_root: Path) -> None:
    client = _client(_daemon(transcript_root, _log()))
    body = client.get(_url(ALPHA, CARD)).json()
    assert body["runs_available"] is True
    implementing = next(stage for stage in body["stages"] if stage["stage"] == "implementing")
    assert implementing["run_count"] == 1
    assert implementing["run_count_source"] == "transcripts"
    assert implementing["duration_seconds"] == 150.0
    assert implementing["runs"][0]["run"] == RUN
    assert implementing["runs"][0]["outcome"] == "completed"


def test_without_transcripts_the_runs_are_not_recorded_and_the_card_still_shows() -> None:
    client = _client(_daemon(None, _log()))
    body = client.get(_url(ALPHA, CARD)).json()
    assert body["runs_available"] is False
    assert body["runs_reason"]
    implementing = next(stage for stage in body["stages"] if stage["stage"] == "implementing")
    assert implementing["run_count"] == 1
    assert implementing["run_count_source"] == "lifecycle"
    assert body["not_recorded"]


def test_tokens_come_from_the_ledger_and_an_unpriced_cost_is_unknown(transcript_root: Path) -> None:
    daemon = _daemon(transcript_root, _log())
    collector = daemon.state["transcript_collector"]
    usage_record = {"seq": 3, "type": "usage", "scope": "message",
                    "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read": 5,
                              "cache_write": 2, "model": "claude-test-model"}}
    collector.usage.observe(RUN, symphony=ALPHA, card=CARD, stage="implementing", records=[usage_record])
    body = _client(daemon).get(_url(ALPHA, CARD)).json()
    implementing = next(stage for stage in body["stages"] if stage["stage"] == "implementing")
    assert implementing["usage"]["tokens"]["total"] == 127
    assert implementing["usage"]["cost"]["usd"] is None
    assert implementing["usage"]["cost"]["unknown_reason"]
    reviewing = next(stage for stage in body["stages"] if stage["stage"] == "reviewing")
    assert reviewing["usage"] is None
    assert reviewing["usage_reason"]


def test_a_card_whose_events_predate_the_log_says_so(transcript_root: Path) -> None:
    empty = CardLifecycleLog()
    body = _client(_daemon(transcript_root, empty)).get(_url(ALPHA, CARD)).json()
    assert body["events"] == []
    assert any("No lifecycle events" in note for note in body["not_recorded"])
    assert all(stage["status"] != "done" for stage in body["stages"])


def test_the_board_strip_reads_the_log_and_the_live_stage_per_symphony() -> None:
    log = _log()
    daemon = _daemon(None, log)
    daemon.state["active_sessions"] = {CARD: {"performer_stage": "reviewing", "phase": "monitoring_pr"}}
    block = card_pipeline_block(daemon)
    assert set(block) == {ALPHA, BETA}
    strip = block[ALPHA][CARD]
    assert strip["current"] == "reviewing"
    assert [item["stage"] for item in strip["statuses"]] == [
        "assessing", "architecting", "implementing", "reviewing", "security", "qa", "documenting", "closing_review",
    ]
    assert block[BETA][BETA_CARD]["current"] is None


def test_the_board_strip_is_empty_without_a_log() -> None:
    assert card_pipeline_block(_daemon(None, None)) == {}


def test_the_route_is_deferred_from_the_mcp_surface() -> None:
    assert is_deferred(f"/api/cards/{ALPHA}/{CARD}/timeline")

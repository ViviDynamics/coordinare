"""#211: the run detail route's header takes its tokens and cost from the ledger for that run."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import CostTrackingConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.lib.path_segments import CONDUCTOR_FOLDER
from coordinare.localhost_guard import build_permitted
from coordinare.services.run_transcript import TranscriptCollector, transcript_path

BASE = "http://127.0.0.1:8090"
TOKEN = "a-secret-operator-token-of-at-least-32-characters"
SYMPHONY = "acme"
CARD = "PVTI_211"
RUN = "run-211"
UNKNOWN_RUN = "run-211-unknown"
OPUS = "claude-opus-4-5"
UNPRICED = "gpt-unpriced"
AS_OF = "2026-10-01"
ONE_MILLION = 1_000_000
OPUS_INPUT_USD = 15.0
OPUS_OUTPUT_USD = 75.0
TS = "2026-10-14T10:00:00+00:00"


def _cost_tracking() -> CostTrackingConfig:
    return CostTrackingConfig.model_validate({
        "pricing_as_of": AS_OF,
        "prices": [{"models": [OPUS], "input_per_million": OPUS_INPUT_USD,
                    "output_per_million": OPUS_OUTPUT_USD, "cache_read_per_million": 1.5,
                    "cache_write_per_million": 18.75}],
    })


def _usage(seq: int, *, model: str, input_tokens: int, output_tokens: int = 0) -> dict[str, Any]:
    return {
        "ts": TS, "seq": seq, "type": "usage", "scope": "message",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read": 0,
                  "cache_write": 0, "model": model},
    }


def _write_run(root: Path, *, symphony: str, run: str) -> None:
    # The conductor's folder is not a sanitized name, so transcript_path would rename it; write it as is.
    if symphony == CONDUCTOR_FOLDER:
        path = root / CONDUCTOR_FOLDER / CARD / f"{run}.jsonl"
    else:
        path = transcript_path(root, symphony=symphony, card_id=CARD, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "run_header", "seq": 0, "header_version": 1, "ts": TS, "run_id": run,
              "performer": {"id": "perf-1", "backend": "claude-code"}, "model": {"model": OPUS},
              "card": {"id": CARD, "issue_number": 211, "title": "Exact links"}}
    result = {"ts": TS, "run_id": run, "seq": 1, "type": "result", "is_error": False, "text": "done"}
    path.write_text(json.dumps(header) + "\n" + json.dumps(result) + "\n", encoding="utf-8")


def _client(root: Path, *, enabled: bool = True, priced: bool = True) -> tuple[TestClient, TranscriptCollector]:
    daemon = MagicMock()
    collector = TranscriptCollector(root, enabled=enabled, max_bytes_per_run=10**9, retention_days=0)
    daemon.state = {"startup_complete": True, "phase": "idle", "error_count": 0, "transcript_collector": collector}
    if priced:
        daemon.state["config"] = SimpleNamespace(cost_tracking=_cost_tracking())
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        auth_token=SecretStr(TOKEN),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    client = TestClient(app, base_url=BASE)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return client, collector


def _url(symphony: str, run: str) -> str:
    return f"/api/runs/{symphony}/{CARD}/{run}"


def test_the_header_takes_the_ledger_figures_with_the_price_and_pricing_date(tmp_path: Path) -> None:
    _write_run(tmp_path, symphony=SYMPHONY, run=RUN)
    client, collector = _client(tmp_path)
    collector.usage.observe(RUN, symphony=SYMPHONY, card=CARD, stage="implementing", records=[
        _usage(1, model=OPUS, input_tokens=ONE_MILLION, output_tokens=ONE_MILLION // 2),
    ])
    summary = client.get(_url(SYMPHONY, RUN)).json()["summary"]
    usage = summary["usage"]
    assert usage["tracked"] is True
    assert usage["reason"] is None
    assert usage["tokens"] == {"input": ONE_MILLION, "output": ONE_MILLION // 2, "cache_read": 0,
                               "cache_write": 0, "total": ONE_MILLION + ONE_MILLION // 2}
    # 1 M input at 15 USD plus 0.5 M output at 75 USD.
    assert usage["cost"]["usd"] == OPUS_INPUT_USD + OPUS_OUTPUT_USD / 2
    assert usage["cost"]["unknown_reason"] is None
    row = usage["models"][0]
    assert row["model"] == OPUS
    assert row["price"]["input_per_million"] == OPUS_INPUT_USD
    assert row["price"]["output_per_million"] == OPUS_OUTPUT_USD
    assert row["cost_usd"] == OPUS_INPUT_USD + OPUS_OUTPUT_USD / 2
    assert usage["pricing"] == {"as_of": AS_OF, "source": "table"}


def test_an_unpriced_model_gives_unknown_cost_and_names_it_in_the_header(tmp_path: Path) -> None:
    _write_run(tmp_path, symphony=SYMPHONY, run=RUN)
    client, collector = _client(tmp_path)
    collector.usage.observe(RUN, symphony=SYMPHONY, card=CARD, stage="implementing", records=[
        _usage(1, model=OPUS, input_tokens=ONE_MILLION),
        _usage(2, model=UNPRICED, input_tokens=500),
    ])
    usage = client.get(_url(SYMPHONY, RUN)).json()["summary"]["usage"]
    assert usage["cost"]["usd"] is None
    assert usage["cost"]["partial_usd"] == OPUS_INPUT_USD
    assert UNPRICED in usage["cost"]["unknown_reason"]
    unpriced = next(row for row in usage["models"] if row["model"] == UNPRICED)
    assert unpriced["priced"] is False
    assert unpriced["reason"]


def test_a_run_the_ledger_does_not_know_is_not_tracked(tmp_path: Path) -> None:
    _write_run(tmp_path, symphony=SYMPHONY, run=UNKNOWN_RUN)
    client, _ = _client(tmp_path)
    response = client.get(_url(SYMPHONY, UNKNOWN_RUN))
    assert response.status_code == 200
    usage = response.json()["summary"]["usage"]
    assert usage["tracked"] is False
    assert usage["reason"] == "no usage records in this run"


def test_with_transcripts_disabled_the_header_says_so(tmp_path: Path) -> None:
    _write_run(tmp_path, symphony=SYMPHONY, run=RUN)
    client, _ = _client(tmp_path, enabled=False)
    usage = client.get(_url(SYMPHONY, RUN)).json()["summary"]["usage"]
    assert usage == {"tracked": False, "reason": "transcripts disabled"}


def test_a_conductor_call_gets_its_ledger_figures(tmp_path: Path) -> None:
    _write_run(tmp_path, symphony=CONDUCTOR_FOLDER, run=RUN)
    client, collector = _client(tmp_path)
    collector.usage.observe(RUN, symphony=CONDUCTOR_FOLDER, card=CARD, stage="conductor:persona_scope",
                            records=[_usage(1, model=OPUS, input_tokens=ONE_MILLION)])
    response = client.get(_url(CONDUCTOR_FOLDER, RUN))
    assert response.status_code == 200
    usage = response.json()["summary"]["usage"]
    assert usage["tracked"] is True
    assert usage["tokens"]["input"] == ONE_MILLION
    assert usage["cost"]["usd"] == OPUS_INPUT_USD


def test_the_old_transcript_summed_tokens_and_cost_keys_are_gone(tmp_path: Path) -> None:
    _write_run(tmp_path, symphony=SYMPHONY, run=RUN)
    client, collector = _client(tmp_path)
    collector.usage.observe(RUN, symphony=SYMPHONY, card=CARD, stage="implementing",
                            records=[_usage(1, model=OPUS, input_tokens=1)])
    summary = client.get(_url(SYMPHONY, RUN)).json()["summary"]
    assert "usage" in summary
    assert "tokens" not in summary
    assert "cost" not in summary
    assert {"counts", "outcome", "pr_url", "pr_reason", "duration_seconds"} <= set(summary)

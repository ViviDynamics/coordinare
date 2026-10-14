"""#134: usage capture on the daemon: what the writer hands the ledger, and the poll path.

Covers the writer's ``drain_usage`` (accepted records only, each once, counted
even past the byte cap), the ``stage`` key the daemon adds to usage records, the
live rise of totals across polls, a restart that continues the same file, and a
disabled collector that counts nothing.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from coordinare.models.performer_endpoint import JobStatus, PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_transcript import (
    RunTranscriptWriter,
    TranscriptCollector,
    transcript_path,
)
from coordinare.services.token_usage import TokenLedger
from coordinare.transport.http_transport import PerformerHTTPClient

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
TARGET = {"symphony": "alpha", "card_id": "PVTI_134"}


def _usage(seq: int, *, input_tokens: int, model: str = "claude-opus-4-5") -> dict[str, Any]:
    return {
        "ts": "2026-10-14T10:00:00+00:00",
        "run_id": "performer-side",
        "seq": seq,
        "type": "usage",
        "usage": {"input_tokens": input_tokens, "output_tokens": 5, "cache_read": 0, "cache_write": 0, "model": model},
        "scope": "message",
    }


def _text(seq: int) -> dict[str, Any]:
    return {"ts": "2026-10-14T10:00:00+00:00", "run_id": "performer-side", "seq": seq, "type": "assistant_text", "text": "x"}


def _writer(root: Path, *, max_bytes: int = 10_000_000) -> RunTranscriptWriter:
    return RunTranscriptWriter(root, parts=("alpha", "PVTI_134"), run_id="run-1", max_bytes=max_bytes)


# --- the writer ---------------------------------------------------------------------


def test_drain_returns_the_accepted_usage_records_once(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.set_stage("implementing")
    writer.append_batch([_text(1), _usage(2, input_tokens=10)], last_seq=2)
    drained = writer.drain_usage()
    assert [r["seq"] for r in drained] == [2]
    assert drained[0]["usage"]["input_tokens"] == 10
    assert writer.drain_usage() == []  # taken once


def test_the_stage_is_written_on_usage_records_only(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.set_stage("reviewing")
    writer.append_batch([_text(1), _usage(2, input_tokens=10)], last_seq=2)
    lines = [json.loads(line) for line in transcript_path(tmp_path, symphony="alpha", card_id="PVTI_134", run_id="run-1").read_text().splitlines()]
    by_seq = {line["seq"]: line for line in lines}
    assert by_seq[2]["stage"] == "reviewing"
    assert "stage" not in by_seq[1]


def test_without_a_stage_no_stage_key_is_written(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.append_batch([_usage(1, input_tokens=3)], last_seq=1)
    line = json.loads(transcript_path(tmp_path, symphony="alpha", card_id="PVTI_134", run_id="run-1").read_text())
    assert "stage" not in line


def test_records_refused_by_the_seq_window_are_not_drained(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    # seq 5 is outside the window of a 2-record batch starting at cursor 0.
    writer.append_batch([_usage(5, input_tokens=99), _text(6)], last_seq=6)
    assert writer.drain_usage() == []


def test_usage_past_the_byte_cap_is_still_counted(tmp_path: Path) -> None:
    writer = _writer(tmp_path, max_bytes=300)
    records = [_usage(seq, input_tokens=seq * 10) for seq in range(1, 6)]
    writer.append_batch(records, last_seq=5)
    assert writer.truncated is True
    drained = writer.drain_usage()
    assert [r["seq"] for r in drained] == [1, 2, 3, 4, 5]


def test_the_writer_exposes_its_sanitized_folder_pair(tmp_path: Path) -> None:
    assert _writer(tmp_path).parts == ("alpha", "PVTI_134")


# --- the poll path -------------------------------------------------------------------


class _Client:
    """Serves a fixed list of records by seq, like the performer's transcript buffer."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.last_seq = max((r["seq"] for r in records), default=None)

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
        batch = [r for r in self.records if transcript_after is not None and r["seq"] > transcript_after]
        return JobStatus(
            job_id=job_id,
            state="running",
            started_at=datetime.now(UTC),
            transcript=batch[:transcript_limit],
            transcript_last_seq=self.last_seq,
        )


def _service(client: _Client, collector: TranscriptCollector) -> HTTPPerformerService:
    config = PerformerEndpointConfig(
        id="perf-134",
        mode="persistent",
        image="performer:full",
        endpoint="http://127.0.0.1:9",
        roles=["implementer"],
    )
    return HTTPPerformerService(config, client=cast("PerformerHTTPClient", client), transcripts=collector)


def _collector(root: Path, *, enabled: bool = True) -> TranscriptCollector:
    return TranscriptCollector(root, enabled=enabled, max_bytes_per_run=10_000_000)


async def test_totals_rise_on_each_poll_while_the_run_is_in_progress(tmp_path: Path) -> None:
    client = _Client([_text(1), _usage(2, input_tokens=100), _usage(3, input_tokens=40)])
    collector = _collector(tmp_path / "t")
    service = _service(client, collector)
    payload = {"transcript_target": {**TARGET, "stage": "implementing"}}

    client.records = [_text(1), _usage(2, input_tokens=100)]
    client.last_seq = 2
    await service.check_status("run-1", payload=payload)
    first = collector.usage.totals(run_id="run-1")["claude-opus-4-5"].input_tokens

    client.records = [_text(1), _usage(2, input_tokens=100), _usage(3, input_tokens=40)]
    client.last_seq = 3
    await service.check_status("run-1", payload=payload)
    second = collector.usage.totals(run_id="run-1")["claude-opus-4-5"].input_tokens

    assert (first, second) == (100, 140)
    assert collector.usage.totals(stage="implementing", card="PVTI_134", symphony="alpha")["claude-opus-4-5"].input_tokens == 140


async def test_a_redelivered_batch_is_not_counted_twice(tmp_path: Path) -> None:
    client = _Client([_usage(1, input_tokens=7)])
    collector = _collector(tmp_path / "t")
    service = _service(client, collector)
    payload = {"transcript_target": TARGET}
    await service.check_status("run-1", payload=payload)
    await service.check_status("run-1", payload=payload)
    assert collector.usage.totals(run_id="run-1")["claude-opus-4-5"].input_tokens == 7


async def test_a_restart_continues_the_run_without_double_counting(tmp_path: Path) -> None:
    root = tmp_path / "t"
    client = _Client([_usage(1, input_tokens=100), _usage(2, input_tokens=50)])
    before = _collector(root)
    await _service(client, before).check_status("run-1", payload={"transcript_target": {**TARGET, "stage": "implementing"}})
    counted = before.usage.totals(run_id="run-1")["claude-opus-4-5"].input_tokens
    assert counted == 150

    # The daemon restarts: a fresh ledger is rebuilt from disk, and the writer reads its cursor from the file.
    after = _collector(root)
    after.usage.load_from_transcripts(root, now=NOW)
    assert after.usage.totals(run_id="run-1")["claude-opus-4-5"].input_tokens == 150

    client.records.append(_usage(3, input_tokens=25))
    client.last_seq = 3
    await _service(client, after).check_status("run-1", payload={"transcript_target": {**TARGET, "stage": "implementing"}})
    assert after.usage.totals(run_id="run-1")["claude-opus-4-5"].input_tokens == 175
    assert after.usage.totals(stage="implementing")["claude-opus-4-5"].input_tokens == 175


async def test_disabled_transcripts_count_nothing(tmp_path: Path) -> None:
    client = _Client([_usage(1, input_tokens=100)])
    collector = _collector(tmp_path / "t", enabled=False)
    await _service(client, collector).check_status("run-off", payload={"transcript_target": TARGET})
    assert collector.enabled is False
    assert collector.usage.totals() == {}
    assert not (tmp_path / "t").exists()


def test_the_collector_owns_one_ledger() -> None:
    collector = _collector(Path("/nonexistent-134-root"), enabled=False)
    assert isinstance(collector.usage, TokenLedger)


def test_the_monitor_names_the_stage_in_the_transcript_target() -> None:
    """The stage travels with the target, so the daemon can write it on usage records."""
    from coordinare.graph.nodes.monitor.body import _transcript_target

    target = _transcript_target({"current_symphony": "alpha", "performer_stage": "reviewing"}, "PVTI_134")
    assert target == {"symphony": "alpha", "card_id": "PVTI_134", "stage": "reviewing"}


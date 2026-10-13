"""Spec 129: daemon-side run transcripts (writer, redaction, cap, retention, delivery)."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from coordinare.config import ProjectConfiguration, TranscriptsConfig
from coordinare.models.performer_endpoint import JobStatus, PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_transcript import (
    RunTranscriptWriter,
    TranscriptCollector,
    prune_expired,
    resolve_transcripts_root,
    sanitize_segment,
    transcript_path,
)
from coordinare.transport.http_transport import PerformerHTTPClient

GH_TOKEN = "ghp_" + "c" * 36
BEARER = "Bearer " + "y" * 30
ANTHROPIC_KEY = "sk-ant-" + "z" * 95
AWS_KEY = "AKIA" + "A" * 16


def _rec(seq: int, record_type: str = "assistant_text", **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": "2026-10-13T00:00:00+00:00",
        "run_id": "performer-side-id",
        "seq": seq,
        "type": record_type,
        "text": f"message {seq}",
    }
    base.update(fields)
    return base


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _writer(path: Path, *, max_bytes: int = 10_000_000) -> RunTranscriptWriter:
    return RunTranscriptWriter(path, run_id="run-1", max_bytes=max_bytes)


# --- schema, paths and the root ------------------------------------------------


def test_sanitize_segment_cannot_escape_or_be_empty() -> None:
    assert sanitize_segment("../../etc/passwd") == "etc_passwd"
    assert "/" not in sanitize_segment("a/b\\c")
    assert sanitize_segment("..") == "unattributed"
    assert sanitize_segment("   ") == "unattributed"
    assert sanitize_segment("PVTI_lADO-1.2") == "PVTI_lADO-1.2"


def test_transcript_path_stays_under_root(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    path = transcript_path(
        root, symphony="../../outside", card_id="/abs/card", run_id="run/../x",
    )
    assert path.is_relative_to(root.resolve())
    assert path.suffix == ".jsonl"
    assert path.parent.parent.name == "outside"
    assert path.parent.name == "abs_card"


def test_default_root_follows_attempt_log_base(tmp_path: Path) -> None:
    attempt_base = tmp_path / "config-dir"
    state_base = tmp_path / "state"
    root = resolve_transcripts_root(None, attempt_base=attempt_base, state_base=state_base)
    assert root == attempt_base / "logs" / "transcripts"


def test_explicit_performer_log_dir_is_used(tmp_path: Path) -> None:
    log_dir = tmp_path / "perf-logs"
    root = resolve_transcripts_root(
        log_dir, attempt_base=tmp_path / "cfg", state_base=tmp_path / "state",
    )
    assert root == log_dir / "transcripts"


def test_config_defaults_and_forbids_unknown_keys() -> None:
    cfg = TranscriptsConfig()
    assert cfg.enabled is True
    assert cfg.retention_days == 30
    assert cfg.max_bytes_per_run == 50 * 1024 * 1024
    with pytest.raises(ValueError, match="extra"):
        TranscriptsConfig.model_validate({"enabled": True, "bogus": 1})
    assert ProjectConfiguration.model_fields["transcripts"].default_factory is not None


# --- writer: dedupe, restart, torn line, redaction, cap ----------------------


def test_redelivered_batch_writes_no_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    writer = _writer(path)
    batch = [_rec(1), _rec(2), _rec(3)]
    assert writer.append_batch(batch) == 3
    assert writer.append_batch(batch) == 0
    assert writer.append_batch([_rec(2), _rec(3), _rec(4)]) == 1
    assert [r["seq"] for r in _lines(path)] == [1, 2, 3, 4]
    assert writer.cursor == 4


def test_restarted_writer_resumes_from_file_without_loss_or_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    first = _writer(path)
    first.append_batch([_rec(1), _rec(2), _rec(3)])
    # A brand new writer over the same file stands in for a restarted daemon.
    second = _writer(path)
    assert second.cursor == 3
    assert second.append_batch([_rec(2), _rec(3), _rec(4), _rec(5)]) == 2
    assert [r["seq"] for r in _lines(path)] == [1, 2, 3, 4, 5]


def test_torn_final_line_is_trimmed_before_appending(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    _writer(path).append_batch([_rec(1), _rec(2)])
    with path.open("ab") as handle:
        handle.write(b'{"seq":3,"type":"assistant_te')  # crash mid-write
    writer = _writer(path)
    assert writer.cursor == 2
    writer.append_batch([_rec(3)])
    assert [r["seq"] for r in _lines(path)] == [1, 2, 3]


def test_secrets_are_redacted_before_they_reach_disk(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    writer = _writer(path)
    writer.append_batch([
        _rec(1, text=f"push with {GH_TOKEN} now"),
        _rec(2, "tool_call", tool_name="Bash", call_id="c1",
             input={"command": f"curl -H 'Authorization: {BEARER}' https://api"}),
        _rec(3, "tool_result", call_id="c1",
             output=f"key={ANTHROPIC_KEY} aws={AWS_KEY} {GH_TOKEN}"),
        _rec(4, "assistant_text", text="sensitive key masked", input={"api_key": "plain-value"}),
    ])
    raw = path.read_text()
    for secret in (GH_TOKEN, BEARER, ANTHROPIC_KEY, AWS_KEY, "plain-value"):
        assert secret not in raw
    assert "[REDACTED]" in raw
    assert "***REDACTED***" in raw
    assert all(r["run_id"] == "run-1" for r in _lines(path))


def test_byte_cap_writes_one_truncated_marker_and_counts_drops(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    writer = _writer(path, max_bytes=700)
    batch = [_rec(i, text="w" * 120) for i in range(1, 11)]
    writer.append_batch(batch)
    records = _lines(path)
    kinds = [r["type"] for r in records]
    assert kinds.count("truncated") == 1
    assert kinds[-1] == "truncated"
    assert writer.truncated is True
    assert writer.dropped_records == 10 - (len(records) - 1)
    # A later batch from the performer adds nothing, and never a second marker.
    assert writer.append_batch([_rec(11, text="late")]) == 0
    assert _lines(path)[-1]["type"] == "truncated"
    assert sum(1 for r in _lines(path) if r["type"] == "truncated") == 1
    # The marker survives a restart, so the next writer knows the run is capped.
    assert _writer(path, max_bytes=700).truncated is True


def test_full_text_is_never_truncated_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    big = "tool output line\n" * 2000
    _writer(path).append_batch([_rec(1, "tool_result", output=big, call_id="c9")])
    [record] = _lines(path)
    assert record["output"] == big


def test_invalid_records_are_skipped_not_written(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    writer = _writer(path)
    assert writer.append_batch([{"seq": 1, "type": "not_a_real_type"}]) == 0
    assert not path.exists()


# --- collector: release and writer identity ----------------------------------


def test_collector_reuses_writer_per_run_and_release_keeps_file(tmp_path: Path) -> None:
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    first = collector.writer_for(run_id="sess-1", symphony="alpha", card_id="PVTI_1")
    assert collector.writer_for(run_id="sess-1", symphony="alpha", card_id="PVTI_1") is first
    first.append_batch([_rec(1)])
    collector.release("sess-1")
    assert first.path.exists()
    assert first.path.parent.name == "PVTI_1"
    assert first.path.parent.parent.name == "alpha"


# --- retention ----------------------------------------------------------------


def _age(path: Path, days: int) -> None:
    old = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (old, old))


def test_retention_removes_only_old_transcripts_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    old = transcript_path(root, symphony="s", card_id="c1", run_id="old")
    new = transcript_path(root, symphony="s", card_id="c2", run_id="new")
    _writer(old).append_batch([_rec(1)])
    _writer(new).append_batch([_rec(1)])
    _age(old, days=45)
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text("{}\n")
    _age(outside, days=400)
    removed = prune_expired(root, retention_days=30)
    assert removed == 1
    assert not old.exists()
    assert new.exists()
    assert outside.exists(), "retention must never touch files outside the root"
    assert not (root / "s" / "c1").exists(), "empty card folders are cleaned up"


def test_retention_zero_keeps_everything(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    path = transcript_path(root, symphony="s", card_id="c", run_id="ancient")
    _writer(path).append_batch([_rec(1)])
    _age(path, days=10_000)
    assert prune_expired(root, retention_days=0) == 0
    assert path.exists()


def test_retention_does_not_follow_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    root.mkdir()
    target = tmp_path / "precious.txt"
    target.write_text("keep me")
    _age(target, days=400)
    link = root / "link.jsonl"
    os.symlink(target, link)
    prune_expired(root, retention_days=1)
    assert target.exists()


# --- delivery through the HTTP performer service ---------------------------------


class _FakeClient:
    """Stands in for PerformerHTTPClient. Serves a fixed record list by seq."""

    def __init__(self, records: list[dict[str, Any]], *, last_seq: int | None) -> None:
        self._records = records
        self._last_seq = last_seq
        self.calls: list[dict[str, Any]] = []

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
        self.calls.append({"job_id": job_id, "transcript_after": transcript_after})
        batch: list[dict[str, Any]] = []
        if transcript_after is not None:
            batch = [r for r in self._records if r["seq"] > transcript_after][:transcript_limit]
        return JobStatus(
            job_id=job_id,
            state="running",
            started_at=datetime.now(UTC),
            transcript=batch,
            transcript_last_seq=self._last_seq,
        )


def _service(client: _FakeClient, collector: TranscriptCollector | None) -> HTTPPerformerService:
    config = PerformerEndpointConfig(
        id="perf-129", mode="persistent", image="performer:full",
        endpoint="http://127.0.0.1:9", roles=["implementer"],
    )
    fake = cast("PerformerHTTPClient", client)
    return HTTPPerformerService(config, client=fake, transcripts=collector)


async def test_status_poll_writes_incrementally_and_dedupes_redelivery(tmp_path: Path) -> None:
    records = [_rec(i) for i in range(1, 4)]
    client = _FakeClient(records, last_seq=3)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    payload = {"transcript_target": {"symphony": "alpha", "card_id": "PVTI_9"}}

    await service.check_status("sess-x", payload=payload)
    await service.check_status("sess-x", payload=payload)

    path = transcript_path(tmp_path / "t", symphony="alpha", card_id="PVTI_9", run_id="sess-x")
    assert [r["seq"] for r in _lines(path)] == [1, 2, 3]
    assert client.calls[0]["transcript_after"] == 0
    assert client.calls[1]["transcript_after"] == 3


async def test_disabled_transcripts_add_nothing_to_the_wire(tmp_path: Path) -> None:
    client = _FakeClient([_rec(1)], last_seq=1)
    collector = TranscriptCollector(tmp_path / "t", enabled=False, max_bytes_per_run=10**6)
    service = _service(client, collector)
    await service.check_status("sess-off", payload={})
    assert client.calls[0]["transcript_after"] is None
    assert not (tmp_path / "t").exists()


async def test_terminal_drain_takes_the_tail_before_teardown(tmp_path: Path) -> None:
    records = [_rec(i) for i in range(1, 8)]

    class _Paged(_FakeClient):
        async def get_job(
            self,
            job_id: str,
            *,
            transcript_after: int | None = None,
            transcript_limit: int = 200,
        ) -> JobStatus:
            # Serve at most 2 records per call so the drain must loop.
            return await super().get_job(
                job_id, transcript_after=transcript_after, transcript_limit=2,
            )

    client = _Paged(records, last_seq=7)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    writer = collector.writer_for(run_id="sess-d", symphony="alpha", card_id="PVTI_d")
    first = await service._get_job_status(
        cast("PerformerHTTPClient", client), "sess-d", "sess-d", writer,
    )
    await service._drain_transcript(
        cast("PerformerHTTPClient", client), "sess-d", "sess-d", writer, first,
    )
    assert [r["seq"] for r in _lines(writer.path)] == [1, 2, 3, 4, 5, 6, 7]


async def test_older_performer_without_transcript_fields_writes_nothing(tmp_path: Path) -> None:
    client = _FakeClient([_rec(1)], last_seq=None)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    await service.check_status("sess-old", payload={})
    assert not any((tmp_path / "t").rglob("*.jsonl"))

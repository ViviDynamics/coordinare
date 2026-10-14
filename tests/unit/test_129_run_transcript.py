"""Spec 129: daemon-side run transcripts.

Covers the writer (dedupe, restart, torn line, seq validation, size bounds,
symlink refusal, best-effort failure), redaction on disk, the root placement
rules, the byte cap, retention, and HTTP delivery (incremental, re-delivery,
drain, teardown bound, disabled).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from coordinare.config import ProjectConfiguration, TranscriptsConfig
from coordinare.models.performer_endpoint import JobStatus, PerformerEndpointConfig
from coordinare.services import http_performer_service as hps
from coordinare.services import run_transcript as rt
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_transcript import (
    RootGuard,
    RunTranscriptWriter,
    TranscriptCollector,
    make_collector,
    prune_expired,
    resolve_transcripts_root,
    sanitize_segment,
    transcript_path,
    transcript_root_conflicts,
)
from coordinare.transport.http_transport import PerformerHTTPClient

GH_TOKEN = "ghp_" + "c" * 36
BEARER = "Bearer " + "y" * 30
ANTHROPIC_KEY = "sk-ant-" + "z" * 95
OPENAI_PROJ_KEY = "sk-proj-" + "p" * 40
AWS_KEY = "AKIA" + "A" * 16
EXACT_JOB_SECRET = "jobsecret-7f3a91c2"  # matches no pattern: only exact-value redaction removes it


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


def _writer(root: Path, *, max_bytes: int = 10_000_000) -> RunTranscriptWriter:
    return RunTranscriptWriter(
        root,
        parts=("alpha", "PVTI_1"),
        run_id="run-1",
        max_bytes=max_bytes,
    )


def _file(root: Path) -> Path:
    return root / "alpha" / "PVTI_1" / "run-1.jsonl"


# --- schema, paths and root placement ----------------------------------------


def test_sanitize_segment_cannot_escape_or_be_empty() -> None:
    # A name that had to change carries a digest, so it cannot collide with another id.
    cleaned = sanitize_segment("../../etc/passwd")
    assert cleaned.startswith("etc_passwd-")
    assert "/" not in cleaned
    assert "/" not in sanitize_segment("a/b\\c")
    assert sanitize_segment("..") == "unattributed"
    assert sanitize_segment("   ") == "unattributed"
    assert sanitize_segment("PVTI_lADO-1.2") == "PVTI_lADO-1.2"


def test_transcript_path_is_lexical_and_stays_under_root(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    path = transcript_path(root, symphony="../../outside", card_id="/abs/card", run_id="run/../x")
    assert path.is_relative_to(root)
    assert path.parent.parent.name.startswith("outside-")
    assert path.parent.name.startswith("abs_card-")


def test_default_root_is_daemon_only_state_dir(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    root = resolve_transcripts_root(None, config_dir=tmp_path / "cfg", state_dir=state_dir)
    assert root == state_dir / "transcripts"


def test_relative_override_resolves_against_config_dir(tmp_path: Path) -> None:
    root = resolve_transcripts_root(
        Path("custom/t"),
        config_dir=tmp_path / "cfg",
        state_dir=tmp_path / "state",
    )
    assert root == tmp_path / "cfg" / "custom" / "t"


def test_root_inside_a_performer_mount_is_refused(tmp_path: Path) -> None:
    """HIGH 1: a root under performer_log_dir is readable by every performer container."""
    log_dir = tmp_path / "perf-logs"
    root = log_dir / "transcripts"
    collector = make_collector(
        enabled=True,
        root=root,
        max_bytes_per_run=10**6,
        retention_days=30,
        guard=RootGuard(performer_mounts=(log_dir,)),
    )
    assert collector.enabled is False


def test_mount_inside_the_root_is_refused_too(tmp_path: Path) -> None:
    root = tmp_path / "state" / "transcripts"
    mount = root / "shared"
    guard = RootGuard(performer_mounts=(mount,))
    assert transcript_root_conflicts(root, guard) == [mount.resolve()]


def test_clean_root_stays_enabled_and_is_not_created(tmp_path: Path) -> None:
    root = tmp_path / "state" / "transcripts"
    collector = make_collector(
        enabled=True,
        root=root,
        max_bytes_per_run=10**6,
        retention_days=30,
        guard=RootGuard(performer_mounts=(tmp_path / "perf-logs",)),
    )
    assert collector.enabled is True
    assert not root.exists(), "construction must not create the root"


def test_disabled_touches_nothing_on_disk(tmp_path: Path) -> None:
    """LOW 8: enabled false creates no directory, no probe file, no retention sweep."""
    root = tmp_path / "state" / "transcripts"
    collector = make_collector(
        enabled=False,
        root=root,
        max_bytes_per_run=10**6,
        retention_days=30,
        guard=RootGuard(),
    )
    assert collector.enabled is False
    assert not (tmp_path / "state").exists()


def test_config_defaults_bounds_and_yaml_dir_alias() -> None:
    cfg = TranscriptsConfig()
    assert cfg.enabled is True
    assert cfg.retention_days == 30
    assert cfg.max_bytes_per_run == 50 * 1024 * 1024
    with pytest.raises(ValueError, match="extra"):
        TranscriptsConfig.model_validate({"enabled": True, "bogus": 1})
    assert TranscriptsConfig.model_validate({"dir": "/x/y"}).directory == Path("/x/y")
    with pytest.raises(ValueError, match="max_bytes_per_run"):
        TranscriptsConfig.model_validate({"max_bytes_per_run": 10**12})
    with pytest.raises(ValueError, match="retention_days"):
        TranscriptsConfig.model_validate({"retention_days": 10**6})
    assert ProjectConfiguration.model_fields["transcripts"].default_factory is not None


# --- writer: dedupe, restart, torn line -------------------------------------


def test_redelivered_batch_writes_no_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "t"
    writer = _writer(root)
    batch = [_rec(1), _rec(2), _rec(3)]
    assert writer.append_batch(batch, last_seq=3) == 3
    assert writer.append_batch(batch, last_seq=3) == 0
    assert writer.append_batch([_rec(2), _rec(3), _rec(4)], last_seq=4) == 1
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2, 3, 4]
    assert writer.cursor == 4


def test_restarted_writer_resumes_from_file_without_loss_or_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _writer(root).append_batch([_rec(1), _rec(2), _rec(3)], last_seq=3)
    second = _writer(root)  # a new daemon process over the same file
    second.ensure_loaded()
    assert second.cursor == 3
    assert second.append_batch([_rec(2), _rec(3), _rec(4), _rec(5)], last_seq=5) == 2
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2, 3, 4, 5]


def test_torn_final_line_is_trimmed_before_appending(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _writer(root).append_batch([_rec(1), _rec(2)], last_seq=2)
    with _file(root).open("ab") as handle:
        handle.write(b'{"seq":3,"type":"assistant_te')  # crash mid-write
    writer = _writer(root)
    writer.ensure_loaded()
    assert writer.cursor == 2
    writer.append_batch([_rec(3)], last_seq=3)
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2, 3]


# --- MEDIUM 5: seq validation and size bounds --------------------------------


def test_poisoned_seq_does_not_drop_later_records(tmp_path: Path) -> None:
    """MEDIUM 5: one record with seq 10**18 must not move the cursor past real records."""
    root = tmp_path / "t"
    writer = _writer(root)
    writer.append_batch([_rec(10**18), _rec(1), _rec(2), _rec(3)], last_seq=3)
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2, 3]
    assert writer.cursor == 3


def test_seq_above_the_reported_last_seq_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "t"
    writer = _writer(root)
    writer.append_batch([_rec(1), _rec(2), _rec(50)], last_seq=2)
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2]


def test_out_of_order_batch_is_sorted_and_written_whole(tmp_path: Path) -> None:
    """Review item 8: [1, 3, 2] is written as 1, 2, 3, never partially as 1, 3."""
    root = tmp_path / "t"
    _writer(root).append_batch([_rec(1), _rec(3), _rec(2)], last_seq=3)
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2, 3]


def test_reversed_batch_is_written_whole(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _writer(root).append_batch([_rec(3), _rec(2), _rec(1)], last_seq=3)
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2, 3]


def test_oversize_record_becomes_a_truncation_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rt, "MAX_RECORD_BYTES", 400)
    root = tmp_path / "t"
    writer = _writer(root)
    writer.append_batch([_rec(1, output="x" * 2000), _rec(2)], last_seq=2)
    records = _lines(_file(root))
    assert [r["type"] for r in records] == ["truncated"]
    assert records[0]["seq"] == 1
    assert writer.truncated is True


def test_batch_byte_limit_defers_the_rest_to_the_next_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rt, "MAX_BATCH_BYTES", 300)
    root = tmp_path / "t"
    writer = _writer(root)
    batch = [_rec(i, text="w" * 120) for i in range(1, 5)]
    first = writer.append_batch(batch, last_seq=4)
    assert 1 <= first < 4
    assert writer.cursor == first
    # Each poll re-requests the rest; every poll accepts at least one record.
    for _ in range(4):
        writer.append_batch(batch, last_seq=4)
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2, 3, 4]


# --- byte cap ----------------------------------------------------------------


def test_byte_cap_writes_one_truncated_marker_and_counts_drops(tmp_path: Path) -> None:
    root = tmp_path / "t"
    writer = _writer(root, max_bytes=700)
    writer.append_batch([_rec(i, text="w" * 120) for i in range(1, 11)], last_seq=10)
    records = _lines(_file(root))
    kinds = [r["type"] for r in records]
    assert kinds.count("truncated") == 1
    assert kinds[-1] == "truncated"
    assert writer.truncated is True
    assert writer.dropped_records == 10 - (len(records) - 1)
    assert writer.append_batch([_rec(11, text="late")], last_seq=11) == 0
    assert sum(1 for r in _lines(_file(root)) if r["type"] == "truncated") == 1
    reloaded = _writer(root, max_bytes=700)
    reloaded.ensure_loaded()
    assert reloaded.truncated is True


def test_full_text_is_never_truncated_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "t"
    big = "tool output line\n" * 2000
    _writer(root).append_batch([_rec(1, "tool_result", output=big, call_id="c9")], last_seq=1)
    [record] = _lines(_file(root))
    assert record["output"] == big


def test_invalid_records_are_skipped_not_written(tmp_path: Path) -> None:
    root = tmp_path / "t"
    writer = _writer(root)
    assert writer.append_batch([{"seq": 1, "type": "not_a_real_type"}], last_seq=1) == 0
    assert not _file(root).exists()


# --- MEDIUM 7: usage scope survives the daemon -------------------------------


def test_usage_scope_is_kept_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "t"
    usage = {"input_tokens": 3, "output_tokens": 40, "model": "m"}
    _writer(root).append_batch(
        [
            _rec(1, "usage", usage=usage, scope="message"),
            _rec(2, "usage", usage=usage, scope="run"),
        ],
        last_seq=2,
    )
    scopes = [r["scope"] for r in _lines(_file(root))]
    assert scopes == ["message", "run"]


# --- HIGH 2: symlinks are never followed -------------------------------------


def test_symlinked_card_directory_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    """HIGH 2: a planted directory link must not redirect the write to another card."""
    root = tmp_path / "t"
    victim = tmp_path / "victim-card"
    victim.mkdir()
    root.mkdir()
    (root / "alpha").mkdir()
    os.symlink(victim, root / "alpha" / "PVTI_1")
    writer = _writer(root)
    assert writer.append_batch([_rec(1)], last_seq=1) == 0
    assert writer.failed is True
    assert list(victim.iterdir()) == []


def test_planted_run_file_symlink_is_refused_without_writing_the_target(tmp_path: Path) -> None:
    """HIGH 2: a run file that links to another card's transcript is never appended to."""
    root = tmp_path / "t"
    other = tmp_path / "other-card.jsonl"
    other.write_text('{"seq":1}\n')
    (root / "alpha" / "PVTI_1").mkdir(parents=True)
    os.symlink(other, _file(root))
    writer = _writer(root)
    assert writer.append_batch([_rec(2)], last_seq=2) == 0
    assert other.read_text() == '{"seq":1}\n'
    assert writer.failed is True


def test_symlink_pointing_outside_the_root_does_not_raise(tmp_path: Path) -> None:
    """HIGH 2: this used to surface as an uncaught ValueError on the poll path."""
    root = tmp_path / "t"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    os.symlink(outside, root / "alpha")
    writer = _writer(root)
    assert writer.append_batch([_rec(1)], last_seq=1) == 0
    assert list(outside.iterdir()) == []


# --- HIGH 3: writer failures are contained -----------------------------------


def test_root_that_is_a_file_fails_the_run_without_raising(tmp_path: Path) -> None:
    """HIGH 3: NotADirectoryError used to escape the status poll."""
    root = tmp_path / "t"
    root.write_text("a file, not a directory")
    writer = _writer(root)
    assert writer.append_batch([_rec(1)], last_seq=1) == 0
    assert writer.failed is True
    assert writer.append_batch([_rec(2)], last_seq=2) == 0


def test_collector_writer_never_raises_for_a_broken_root(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.write_text("file")
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    writer = collector.writer_for(run_id="r", symphony="s", card_id="c")
    assert writer.append_batch([_rec(1)], last_seq=1) == 0


# --- HIGH 4: redaction, asserted on the bytes on disk ------------------------


def test_every_secret_shape_is_absent_from_the_bytes_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "t"
    writer = _writer(root)
    writer.add_secret_values([EXACT_JOB_SECRET])
    writer.append_batch(
        [
            _rec(1, text=f"push with {GH_TOKEN} now"),
            _rec(
                2,
                "tool_call",
                tool_name="Bash",
                call_id="c1",
                input={"command": f"curl -H 'Authorization: {BEARER}' https://api"},
            ),
            _rec(
                3,
                "tool_result",
                call_id="c1",
                output=f"key={ANTHROPIC_KEY} aws={AWS_KEY} {GH_TOKEN} {OPENAI_PROJ_KEY}",
            ),
            _rec(
                4,
                "assistant_text",
                text="sensitive key masked",
                input={
                    "api_key": "plain-value",
                    "api-key": "dash-value",
                    "ANTHROPIC_AUTH_TOKEN": "anth-value-1234",
                    "github_token": "gh-value-5678",
                    "input_tokens": 12,
                },
            ),
            _rec(5, "tool_result", output={"s": json.dumps({"password": "pw-in-json-99"})}),
            _rec(
                6,
                "tool_result",
                output="env dump:\nANTHROPIC_AUTH_TOKEN=env-token-abc123\nPATH=/bin",
            ),
            _rec(7, "tool_result", output=f"leaked {EXACT_JOB_SECRET} in a log line"),
        ],
        last_seq=7,
    )
    raw = _file(root).read_bytes()
    for secret in (
        GH_TOKEN,
        BEARER,
        ANTHROPIC_KEY,
        AWS_KEY,
        OPENAI_PROJ_KEY,
        EXACT_JOB_SECRET,
        b"plain-value",
        b"dash-value",
        b"anth-value-1234",
        b"gh-value-5678",
        b"pw-in-json-99",
        b"env-token-abc123",
    ):
        needle = secret if isinstance(secret, bytes) else secret.encode()
        assert needle not in raw, needle
    assert b"[REDACTED]" in raw
    records = _lines(_file(root))
    assert records[3]["input"]["input_tokens"] == 12, "usage counts are not secrets"
    assert records[3]["input"]["api_key"] == "[REDACTED]"
    assert "PATH=/bin" in records[5]["output"], "non-secret env lines are kept"


def test_daemon_collector_redacts_ambient_credentials(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = TranscriptCollector(
        root,
        enabled=True,
        max_bytes_per_run=10**6,
        ambient_secrets={"MY_SERVICE_PASSWORD": "ambient-value-xyz"},
    )
    writer = collector.writer_for(run_id="run-1", symphony="alpha", card_id="PVTI_1")
    writer.append_batch([_rec(1, text="saw ambient-value-xyz here")], last_seq=1)
    assert b"ambient-value-xyz" not in _file(root).read_bytes()


# --- retention ----------------------------------------------------------------


def _age(path: Path, days: int) -> None:
    old = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (old, old))


def test_retention_removes_only_old_transcripts_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    old = transcript_path(root, symphony="s", card_id="c1", run_id="old")
    new = transcript_path(root, symphony="s", card_id="c2", run_id="new")
    for path in (old, new):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"seq":1}\n')
    _age(old, days=45)
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text("{}\n")
    _age(outside, days=400)
    assert prune_expired(root, retention_days=30) == 1
    assert not old.exists()
    assert new.exists()
    assert outside.exists(), "retention must never touch files outside the root"
    assert not (root / "s" / "c1").exists(), "empty card folders are cleaned up"


def test_retention_zero_keeps_everything(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    path = transcript_path(root, symphony="s", card_id="c", run_id="ancient")
    path.parent.mkdir(parents=True)
    path.write_text('{"seq":1}\n')
    _age(path, days=10_000)
    assert prune_expired(root, retention_days=0) == 0
    assert path.exists()


def test_retention_does_not_follow_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    root.mkdir()
    target = tmp_path / "precious.txt"
    target.write_text("keep me")
    _age(target, days=400)
    os.symlink(target, root / "link.jsonl")
    prune_expired(root, retention_days=1)
    assert target.exists()


# --- delivery through the HTTP performer service -------------------------------


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
        id="perf-129",
        mode="persistent",
        image="performer:full",
        endpoint="http://127.0.0.1:9",
        roles=["implementer"],
    )
    return HTTPPerformerService(
        config,
        client=cast("PerformerHTTPClient", client),
        transcripts=collector,
    )


async def test_status_poll_writes_incrementally_and_dedupes_redelivery(tmp_path: Path) -> None:
    root = tmp_path / "t"
    client = _FakeClient([_rec(i) for i in range(1, 4)], last_seq=3)
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    payload = {"transcript_target": {"symphony": "alpha", "card_id": "PVTI_9"}}
    await service.check_status("sess-x", payload=payload)
    await service.check_status("sess-x", payload=payload)
    path = transcript_path(root, symphony="alpha", card_id="PVTI_9", run_id="sess-x")
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


async def test_terminal_drain_takes_the_tail(tmp_path: Path) -> None:
    root = tmp_path / "t"

    class _Paged(_FakeClient):
        async def get_job(
            self,
            job_id: str,
            *,
            transcript_after: int | None = None,
            transcript_limit: int = 200,
        ) -> JobStatus:
            return await super().get_job(
                job_id,
                transcript_after=transcript_after,
                transcript_limit=2,
            )

    client = _Paged([_rec(i) for i in range(1, 8)], last_seq=7)
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    writer = collector.writer_for(run_id="sess-d", symphony="alpha", card_id="PVTI_d")
    fake = cast("PerformerHTTPClient", client)
    first = await service._get_job_status(fake, "sess-d", "sess-d", writer)
    await service._drain_transcript(fake, "sess-d", writer, first)
    path = transcript_path(root, symphony="alpha", card_id="PVTI_d", run_id="sess-d")
    assert [r["seq"] for r in _lines(path)] == [1, 2, 3, 4, 5, 6, 7]


async def test_older_performer_without_transcript_fields_writes_nothing(tmp_path: Path) -> None:
    client = _FakeClient([_rec(1)], last_seq=None)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    await service.check_status("sess-old", payload={})
    assert not any((tmp_path / "t").rglob("*.jsonl"))


async def test_writer_exception_never_escapes_the_status_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH 3: a transcript failure must not break the status poll."""
    client = _FakeClient([_rec(1)], last_seq=1)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)

    def explode(self: RunTranscriptWriter, *args: Any, **kwargs: Any) -> int:
        message = "disk on fire"
        raise OSError(message)

    monkeypatch.setattr(RunTranscriptWriter, "append_batch", explode)
    payload = {"transcript_target": {"symphony": "alpha", "card_id": "PVTI_x"}}
    status = await service.check_status("sess-boom", payload=payload)
    assert status["status"] == "working"


async def test_release_session_drains_before_teardown(tmp_path: Path) -> None:
    """MEDIUM 6: a cancelled run keeps the records since its last poll."""
    root = tmp_path / "t"
    client = _FakeClient([_rec(i) for i in range(1, 6)], last_seq=5)
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    await service.check_status(
        "sess-rel",
        payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_r"}},
    )
    client._records.extend(_rec(i) for i in range(6, 9))
    client._last_seq = 8
    await service.release_session("sess-rel")
    path = transcript_path(root, symphony="alpha", card_id="PVTI_r", run_id="sess-rel")
    assert [r["seq"] for r in _lines(path)] == [1, 2, 3, 4, 5, 6, 7, 8]


async def test_hanging_performer_cannot_delay_teardown_beyond_its_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM 6: the teardown drain is time-bounded and never prevents the teardown."""
    monkeypatch.setattr(hps, "_TRANSCRIPT_TEARDOWN_SECONDS", 0.05)

    class _Hanging(_FakeClient):
        async def get_job(self, job_id: str, **kwargs: Any) -> JobStatus:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(_FakeClient([], last_seq=3), collector)
    await service.check_status(
        "sess-hang",
        payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_h"}},
    )
    # The performer now stops answering. Only the teardown path is exercised below.
    service._injected_client = cast("PerformerHTTPClient", _Hanging([], last_seq=3))
    started = time.monotonic()
    await service.release_session("sess-hang")
    assert time.monotonic() - started < 5


async def test_terminal_drain_has_a_wall_clock_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH 3: a drain that keeps returning new records is cut off by time, not rounds."""
    monkeypatch.setattr(hps, "_TRANSCRIPT_DRAIN_SECONDS", 0.2)

    class _Endless(_FakeClient):
        async def get_job(self, job_id: str, **kwargs: Any) -> JobStatus:
            await asyncio.sleep(0.02)
            after = int(kwargs.get("transcript_after") or 0)
            return JobStatus(
                job_id=job_id,
                state="running",
                started_at=datetime.now(UTC),
                transcript=[_rec(after + 1)],
                transcript_last_seq=10**9,
            )

    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(_Endless([], last_seq=None), collector)
    writer = collector.writer_for(run_id="sess-e", symphony="alpha", card_id="PVTI_e")
    start_status = JobStatus(
        job_id="sess-e",
        state="running",
        started_at=datetime.now(UTC),
        transcript=[],
        transcript_last_seq=10**9,
    )
    started = time.monotonic()
    await service._drain_transcript(
        cast("PerformerHTTPClient", _Endless([], last_seq=None)),
        "sess-e",
        writer,
        start_status,
        timeout=0.2,
    )
    assert time.monotonic() - started < 3
    assert writer.cursor > 0

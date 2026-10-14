"""Spec 130: the run header written at dispatch.

Covers the writer contract (header first, seq 0 never shifts performer seq, cursor
recovery after a restart, no header into a file that already has records, redaction
on disk, failure containment), the header builder (shape, null-with-reason, bounds),
and the real dispatch path (header before the first performer record, disabled
transcripts leave the payload unchanged, a failed or slow header never fails or
delays dispatch).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from coordinare.models.performer_endpoint import (
    JobAcceptResponse,
    JobStatus,
    PerformerEndpointConfig,
)
from coordinare.services import http_performer_service as hps
from coordinare.services import run_header as rh
from coordinare.services import run_transcript as rt
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_transcript import (
    RunTranscriptWriter,
    TranscriptCollector,
    transcript_path,
)
from coordinare.transport.http_transport import PerformerHTTPClient

GH_TOKEN = "ghp_" + "h" * 36
BEARER = "Bearer " + "q" * 30
EXACT_SECRET = "jobsecret-130-exact"
CARD_ID = "PVTI_130"
SYMPHONY = "alpha"


def _rec(seq: int, text: str | None = None) -> dict[str, Any]:
    return {
        "ts": "2026-10-14T00:00:00+00:00",
        "run_id": "performer-side-id",
        "seq": seq,
        "type": "assistant_text",
        "text": text or f"message {seq}",
    }


def _file(root: Path, run_id: str = "run-130") -> Path:
    return root / "alpha" / "PVTI_130" / f"{run_id}.jsonl"


def _writer(root: Path, run_id: str = "run-130") -> RunTranscriptWriter:
    return RunTranscriptWriter(
        root,
        parts=("alpha", "PVTI_130"),
        run_id=run_id,
        max_bytes=10_000_000,
    )


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _header(**overrides: Any) -> dict[str, Any]:
    """A header as the builder produces it, minus the fields the writer stamps."""
    base: dict[str, Any] = {
        "type": rh.RUN_HEADER_TYPE,
        "seq": 0,
        "header_version": rh.RUN_HEADER_VERSION,
        "ts": "2026-10-14T00:00:00+00:00",
        "run_id": "run-130",
        "card": {"id": CARD_ID, "description": f"token {GH_TOKEN} and {EXACT_SECRET}"},
        "persona": {"name": "implementer", "text": f"Use {BEARER} for the API"},
    }
    base.update(overrides)
    return base


# --- writer: ordering, seq, cursor ------------------------------------------


def test_header_is_the_first_line_and_leaves_performer_seq_alone(tmp_path: Path) -> None:
    root = tmp_path / "t"
    writer = _writer(root)
    writer.stage_run_header(_header())
    assert writer.flush_run_header() is True
    assert writer.append_batch([_rec(1), _rec(2)], last_seq=2) == 2
    lines = _lines(_file(root))
    assert [line["type"] for line in lines] == [rh.RUN_HEADER_TYPE, "assistant_text", "assistant_text"]
    assert [line["seq"] for line in lines] == [0, 1, 2]
    assert lines[0]["run_id"] == "run-130"


def test_a_content_write_puts_a_staged_header_first(tmp_path: Path) -> None:
    """The dispatch-side flush may time out. The first poll must still see the header first."""
    root = tmp_path / "t"
    writer = _writer(root)
    writer.stage_run_header(_header())
    writer.append_batch([_rec(1)], last_seq=1)
    lines = _lines(_file(root))
    assert [line["type"] for line in lines] == [rh.RUN_HEADER_TYPE, "assistant_text"]


def test_a_header_only_file_resumes_the_cursor_at_zero(tmp_path: Path) -> None:
    root = tmp_path / "t"
    writer = _writer(root)
    writer.stage_run_header(_header())
    assert writer.flush_run_header() is True
    restarted = _writer(root)
    assert restarted.cursor == 0
    assert restarted.append_batch([_rec(1)], last_seq=1) == 1
    assert [line["seq"] for line in _lines(_file(root))] == [0, 1]


def test_a_restarted_writer_resumes_after_the_last_performer_record(tmp_path: Path) -> None:
    root = tmp_path / "t"
    writer = _writer(root)
    writer.stage_run_header(_header())
    writer.flush_run_header()
    writer.append_batch([_rec(1), _rec(2)], last_seq=2)
    restarted = _writer(root)
    assert restarted.append_batch([_rec(1), _rec(2), _rec(3)], last_seq=3) == 1
    assert [line["seq"] for line in _lines(_file(root))] == [0, 1, 2, 3]


def test_no_header_is_written_into_a_file_that_already_has_records(tmp_path: Path) -> None:
    root = tmp_path / "t"
    first = _writer(root)
    first.append_batch([_rec(1)], last_seq=1)
    before = _file(root).read_bytes()
    second = _writer(root)
    second.stage_run_header(_header())
    assert second.flush_run_header() is False
    assert _file(root).read_bytes() == before


def test_a_header_write_failure_is_contained_and_records_still_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "t"
    writer = _writer(root)
    real_write = writer._write
    calls: list[int] = []

    def failing_once(payload: bytes) -> None:
        calls.append(len(payload))
        if len(calls) == 1:
            raise OSError("disk full (simulated)")
        real_write(payload)

    monkeypatch.setattr(writer, "_write", failing_once)
    writer.stage_run_header(_header())
    assert writer.flush_run_header() is False  # never raises
    assert writer.failed is False
    assert writer.append_batch([_rec(1)], last_seq=1) == 1
    assert [line["type"] for line in _lines(_file(root))] == ["assistant_text"]


def test_a_root_that_cannot_hold_the_header_fails_quietly(tmp_path: Path) -> None:
    blocked = tmp_path / "t"
    blocked.write_text("not a directory")
    writer = _writer(blocked)
    writer.stage_run_header(_header())
    assert writer.flush_run_header() is False


def test_header_text_is_redacted_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "t"
    writer = _writer(root)
    writer.add_secret_values([EXACT_SECRET])
    writer.stage_run_header(_header())
    writer.flush_run_header()
    raw = _file(root).read_bytes().decode("ascii")
    assert GH_TOKEN not in raw
    assert "q" * 30 not in raw
    assert EXACT_SECRET not in raw
    header = _lines(_file(root))[0]
    assert header["type"] == rh.RUN_HEADER_TYPE
    assert header["card"]["id"] == CARD_ID


# --- builder: shape, reasons, bounds -----------------------------------------

_NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
_PERFORMER: dict[str, str | None] = {"id": "p", "transport": "persistent", "endpoint": None}


def _card_context(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": CARD_ID,
        "issue_number": 130,
        "title": "Run header",
        "description": "Record the inputs.",
        "acceptance_criteria": ["header first", "versions kept"],
        "role": "implementing",
        "backend": "claude_code",
        "model": "claude-sonnet",
        "base_url": None,
        "api_key_env": "ANTHROPIC_API_KEY",
        "relay_feedback": [
            {"kind": "review", "author_login": "rev", "body": "fix the cap", "comments": [{"path": "a.py", "body": "nit"}]},
        ],
    }
    base.update(extra)
    return base


def _run_inputs(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "transcript_target": {"symphony": SYMPHONY, "card_id": CARD_ID},
        "persona": {
            "name": "implementer",
            "version": 3,
            "version_sha256": "ab" * 32,
            "changed_at": "2026-10-13T09:00:00+00:00",
            "changed_by": "Ada",
            "source": "dashboard",
        },
        "mode": {"name": "single", "strategy": "single"},
        "routing": {"role": "implementer", "backend": "claude_code", "rule": "performers.implementer -> mode single"},
    }
    base.update(extra)
    return base


def test_builder_shape_and_sources() -> None:
    header = rh.build_run_header(
        run_id="run-130",
        card_context=_card_context(),
        run_inputs=_run_inputs(),
        performer={"id": "perf-1", "transport": "persistent", "endpoint": "http://perf:8080"},
        now=_NOW,
    )
    assert header["type"] == "run_header"
    assert header["seq"] == 0
    assert header["header_version"] == rh.RUN_HEADER_VERSION
    assert header["run_id"] == "run-130"
    assert header["ts"] == _NOW.isoformat()
    assert header["persona"]["version"] == 3
    assert header["persona"]["version_sha256"] == "ab" * 32
    assert header["persona"]["changed_by"] == "Ada"
    assert header["performer"] == {
        "id": "perf-1", "backend": "claude_code", "transport": "persistent", "endpoint": "http://perf:8080",
    }
    assert header["model"]["model"] == "claude-sonnet"
    assert header["model"]["env_var_names"]["api"] == "ANTHROPIC_API_KEY"
    assert header["card"]["title"] == "Run header"
    assert header["card"]["acceptance_criteria"] == ["header first", "versions kept"]
    assert header["card"]["truncated"] is False
    assert header["feedback"]["items"][0]["body"] == "fix the cap"
    assert header["feedback"]["items"][0]["comments"] == [{"path": "a.py", "body": "nit"}]
    assert header["feedback"]["omitted"] == 0
    assert header["mode"]["name"] == "single"
    assert header["routing"]["rule"] == "performers.implementer -> mode single"


def test_the_prompt_is_not_rendered_by_the_daemon_and_says_so() -> None:
    header = rh.build_run_header(
        run_id="r", card_context=_card_context(), run_inputs=_run_inputs(),
        performer=_PERFORMER, now=_NOW,
    )
    assert header["prompt"]["rendered"] is None
    assert "performer" in header["prompt"]["reason"]
    assert "user" in header["prompt"]["see"]


def test_unknowns_are_null_with_a_reason_not_guessed() -> None:
    header = rh.build_run_header(
        run_id="r", card_context=_card_context(), run_inputs=None,
        performer={"id": "p", "transport": "ephemeral", "endpoint": None}, now=_NOW,
    )
    assert header["persona"]["version"] is None
    assert header["persona"]["reason"]
    assert header["routing"]["rule"] is None
    assert header["routing"]["reason"]
    assert header["routing"]["performer_routing_table"]["rule"] is None
    assert header["routing"]["performer_routing_table"]["reason"]
    assert header["performer"]["endpoint"] is None
    assert header["model_endpoints"]["tool"] is None
    assert header["model_endpoints"]["reason"]


def test_card_text_and_feedback_are_bounded() -> None:
    huge = "x" * (rh.CARD_TEXT_LIMIT_CHARS * 4)
    feedback = [
        {"kind": "review", "body": "y" * (rh.FEEDBACK_ITEM_LIMIT_CHARS * 4)} for _ in range(400)
    ]
    header = rh.build_run_header(
        run_id="r",
        card_context=_card_context(description=huge, relay_feedback=feedback),
        run_inputs=_run_inputs(),
        performer=_PERFORMER, now=_NOW,
    )
    assert len(header["card"]["description"]) < len(huge)
    assert "truncated" in header["card"]["description"]
    assert header["card"]["truncated"] is True
    assert header["feedback"]["omitted"] > 0
    assert len(header["feedback"]["items"]) < len(feedback)
    encoded = json.dumps(header)
    assert len(encoded) <= rh.RUN_HEADER_MAX_BYTES


def test_builder_never_raises_on_odd_input() -> None:
    header = rh.build_run_header(
        run_id="r", card_context={"description": None, "relay_feedback": "not a list"},
        run_inputs={"persona": "not a dict"}, performer=_PERFORMER, now=_NOW,
    )
    assert header["type"] == "run_header"
    assert header["feedback"]["items"] == []


def test_build_run_inputs_without_a_daemon_config_says_why() -> None:
    inputs = rh.build_run_inputs(config=None, role="implementer", persona_sent=None)
    assert inputs["persona"]["version"] is None
    assert inputs["persona"]["reason"]
    assert inputs["routing"]["rule"] is None


# --- transport: the real dispatch path with a fake performer client ----------


class _DispatchClient:
    """Accepts every job and serves a fixed record list (the performer's side)."""

    def __init__(self, records: list[dict[str, Any]], *, last_seq: int | None) -> None:
        self._records = records
        self._last_seq = last_seq
        self.posted: list[Any] = []

    async def post_job(self, payload: Any) -> JobAcceptResponse:
        self.posted.append(payload)
        return JobAcceptResponse(job_id=payload.job_id, started_at=datetime.now(UTC))

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
        batch = []
        if transcript_after is not None:
            batch = [r for r in self._records if r["seq"] > transcript_after][:transcript_limit]
        return JobStatus(
            job_id=job_id,
            state="running",
            started_at=datetime.now(UTC),
            transcript=batch,
            transcript_last_seq=self._last_seq,
        )


def _service(client: _DispatchClient, collector: TranscriptCollector | None) -> HTTPPerformerService:
    config = PerformerEndpointConfig(
        id="perf-130",
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


def _dispatch_context(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": CARD_ID,
        "role": "implementer",
        "backend": "claude_code",
        "repo_url": "https://github.com/example/repo",
        "branch": "coordinare/1/card",
        "title": "Run header",
        "description": "Record the inputs.",
        "acceptance_criteria": ["header first"],
        "model": "claude-sonnet",
        "persona_instructions": "Be precise.",
    }
    base.update(extra)
    return base


async def test_dispatch_writes_the_header_before_the_first_performer_record(tmp_path: Path) -> None:
    root = tmp_path / "t"
    client = _DispatchClient([_rec(1), _rec(2)], last_seq=2)
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    result = await service.dispatch_card(_dispatch_context(), run_inputs=_run_inputs())
    assert result["status"] == "ok"
    run_id = result["session_id"]
    path = transcript_path(root, symphony=SYMPHONY, card_id=CARD_ID, run_id=run_id)
    assert _lines(path)[0]["type"] == "run_header"
    await service.check_status(run_id, payload={"transcript_target": {"symphony": SYMPHONY, "card_id": CARD_ID}})
    lines = _lines(path)
    assert [line["seq"] for line in lines] == [0, 1, 2]
    header = lines[0]
    assert header["performer"]["id"] == "perf-130"
    assert header["persona"]["version"] == 3
    assert header["card"]["id"] == CARD_ID


async def test_a_dispatch_without_a_transcript_target_writes_no_header_and_pins_no_folder(
    tmp_path: Path,
) -> None:
    """Without the folder, no writer may be created at dispatch. The first poll names the file."""
    root = tmp_path / "t"
    client = _DispatchClient([_rec(1)], last_seq=1)
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    result = await service.dispatch_card(_dispatch_context(), run_inputs=None)
    assert result["status"] == "ok"
    assert collector.live_paths() == frozenset()
    assert not root.exists()
    await service.check_status(
        result["session_id"],
        payload={"transcript_target": {"symphony": SYMPHONY, "card_id": CARD_ID}},
    )
    path = transcript_path(root, symphony=SYMPHONY, card_id=CARD_ID, run_id=result["session_id"])
    assert [line["seq"] for line in _lines(path)] == [1]


async def test_disabled_transcripts_leave_the_payload_unchanged(tmp_path: Path) -> None:
    on_wire: list[dict[str, Any]] = []
    for run_inputs in (None, _run_inputs()):
        client = _DispatchClient([], last_seq=None)
        collector = TranscriptCollector(tmp_path / "t", enabled=False, max_bytes_per_run=10**6)
        service = _service(client, collector)
        await service.dispatch_card(_dispatch_context(), run_inputs=run_inputs)
        payload = client.posted[0].model_dump(mode="json")
        payload["job_id"] = "fixed"
        on_wire.append(payload)
    assert on_wire[0] == on_wire[1]
    assert on_wire[0]["transcript_max_bytes"] is None
    assert not (tmp_path / "t").exists()


async def test_a_header_write_failure_never_fails_the_dispatch(tmp_path: Path) -> None:
    blocked = tmp_path / "t"
    blocked.write_text("a file where the root should be")
    client = _DispatchClient([], last_seq=None)
    collector = TranscriptCollector(blocked, enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    result = await service.dispatch_card(_dispatch_context(), run_inputs=_run_inputs())
    assert result["status"] == "ok"
    assert result["accepted"] is True


async def test_a_slow_header_write_is_bounded_and_dispatch_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hps, "RUN_HEADER_WAIT_SECONDS", 0.01)

    def slow_flush(self: RunTranscriptWriter) -> bool:
        time.sleep(0.2)
        return True

    monkeypatch.setattr(rt.RunTranscriptWriter, "flush_run_header", slow_flush)
    client = _DispatchClient([], last_seq=None)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    result = await service.dispatch_card(_dispatch_context(), run_inputs=_run_inputs())
    assert result["status"] == "ok"


async def test_job_secret_in_card_text_is_absent_from_the_header_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "t"
    client = _DispatchClient([_rec(1)], last_seq=1)
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    context = _dispatch_context(
        description=f"the deploy key is {EXACT_SECRET}",
        test_env_vars={"ACME_DEPLOY_KEY": EXACT_SECRET},
    )
    result = await service.dispatch_card(context, run_inputs=_run_inputs())
    path = transcript_path(root, symphony=SYMPHONY, card_id=CARD_ID, run_id=result["session_id"])
    raw = path.read_bytes().decode("ascii")
    assert EXACT_SECRET not in raw
    assert _lines(path)[0]["type"] == "run_header"

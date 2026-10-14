"""Spec 129, third review: one test per finding, each exercising the failing input.

Covers the redaction recursion fallback, the record-level and line-level guarantees,
the non-blocking and short-write-safe file path, the poll and terminal bounds, the
retention races, and the redaction rules added or corrected in this round.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from structlog.testing import capture_logs

from coordinare.daemon import CoordinareDaemon
from coordinare.lib import transcript_redaction as tr
from coordinare.lib.transcript_redaction import redact_text, redact_value
from coordinare.models.performer_endpoint import (
    JobStatus,
    PerformerEndpointConfig,
)
from coordinare.services import http_performer_service as hps
from coordinare.services import run_transcript as rt
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_transcript import (
    RunTranscriptWriter,
    TranscriptCollector,
    prune_expired,
    sanitize_segment,
    transcript_path,
)
from coordinare.transport.http_transport import PerformerHTTPClient

GITHUB_TOKEN = "ghp_" + "q" * 36
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


def _rec(seq: int, text: str = "message") -> dict[str, Any]:
    return {
        "ts": "2026-10-13T00:00:00+00:00",
        "run_id": "performer-side-id",
        "seq": seq,
        "type": "assistant_text",
        "text": f"{text} {seq}",
    }


def _writer(root: Path, *, max_bytes: int = 10_000_000) -> RunTranscriptWriter:
    return RunTranscriptWriter(
        root,
        parts=("alpha", "PVTI_1"),
        run_id="run-1",
        max_bytes=max_bytes,
    )


def _file(root: Path) -> Path:
    return root / "alpha" / "PVTI_1" / "run-1.jsonl"


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- finding 1: recursion ----------------------------------------------------------


def test_deep_brackets_do_not_raise_and_come_back_as_text() -> None:
    """HIGH 1: 1000 `[` then 1000 `]` used to raise RecursionError on the JSON path."""
    text = "[" * 1000 + "]" * 1000
    out = redact_value(text)
    assert isinstance(out, str)


def test_deep_structure_is_bounded_not_recursed_without_limit() -> None:
    nested: Any = "leaf"
    for _ in range(5000):
        nested = [nested]
    out = redact_value({"output": nested})
    assert out is not None


def test_secret_inside_deep_brackets_is_still_redacted() -> None:
    """The fallback redacts the text of the value, never returns it unredacted."""
    text = "[" * 1000 + GITHUB_TOKEN + "]" * 1000
    out = redact_value(text)
    assert GITHUB_TOKEN not in out


# --- finding 2: every line is JSON and keeps its seq --------------------------------

NASTY_STRINGS = [
    "Enter the password:",
    "token: x and password: y",
    "\ud800 lone surrogate",
    "nul \x00 byte",
    "\x1b[31mred\x1b[0m",
    '{"password": "pw-json-value"}',
    "[" * 900 + "]" * 900,
    "-----BEGIN RSA PRIVATE KEY-----",
    GITHUB_TOKEN,
    "emoji \U0001f389 and line\u2028sep",
    "x" * 5000,
    'back\\slash and "quote"',
]


def test_every_line_parses_and_keeps_its_seq_for_nasty_strings(tmp_path: Path) -> None:
    """Review item 2 (property-style): for every nasty string in every string field, each
    written line is valid JSON and carries exactly the seq it was given."""
    root = tmp_path / "t"
    records: list[dict[str, Any]] = []
    for index, text in enumerate(NASTY_STRINGS, start=1):
        records.append(
            {
                "ts": "2026-10-13T00:00:00+00:00",
                "run_id": "r",
                "seq": index,
                "type": "tool_call",
                "text": text,
                "tool_name": text,
                "call_id": text,
                "input": {"command": text, "nested": [text, {"k": text}]},
                "output": text,
            },
        )
    _writer(root).append_batch(records, last_seq=len(records))
    raw_lines = _file(root).read_bytes().splitlines()
    assert len(raw_lines) == len(records)
    parsed = [json.loads(line) for line in raw_lines]
    assert [entry["seq"] for entry in parsed] == list(range(1, len(records) + 1))


def test_line_level_pass_no_longer_corrupts_prose_with_a_marker(tmp_path: Path) -> None:
    """Review item 2: ``Enter the password:`` used to become invalid JSON once the whole
    line was redacted. The value is redacted, and the line stays valid."""
    root = tmp_path / "t"
    _writer(root).append_batch(
        [
            {
                "ts": "x",
                "run_id": "r",
                "seq": 1,
                "type": "assistant_text",
                "text": "Enter the password:",
            },
        ],
        last_seq=1,
    )
    [record] = _lines(_file(root))
    assert record["seq"] == 1
    assert record["text"] == "Enter the password:"


def test_cursor_is_recovered_past_an_unparsable_last_line(tmp_path: Path) -> None:
    """Review item 2: a garbled last line must not reset the cursor to 0 (duplicates)."""
    root = tmp_path / "t"
    _writer(root).append_batch([_rec(1), _rec(2), _rec(3)], last_seq=3)
    with _file(root).open("ab") as handle:
        handle.write(b"this is not json\n")
    writer = _writer(root)
    writer.ensure_loaded()
    assert writer.cursor == 3


# --- finding 3: non-blocking, short-write-safe file path ------------------------------


def test_fifo_at_the_run_path_fails_the_run_without_hanging(tmp_path: Path) -> None:
    """Review item 3a: a FIFO used to hang the worker in os.open. It now fails the run."""
    root = tmp_path / "t"
    _file(root).parent.mkdir(parents=True)
    os.mkfifo(_file(root))
    writer = _writer(root)
    result: dict[str, int] = {}

    def run() -> None:
        result["written"] = writer.append_batch([_rec(1)], last_seq=1)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "the append must not block on a FIFO"
    assert result.get("written") == 0
    assert writer.failed is True


def test_a_failed_load_never_reaches_the_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 3a: after a failed load, no write is attempted."""
    root = tmp_path / "t"
    _file(root).parent.mkdir(parents=True)
    os.mkfifo(_file(root))
    writer = _writer(root)
    opened: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(rt.os, "open", spy)
    assert writer.append_batch([_rec(1)], last_seq=1) == 0
    assert all(not (flags & os.O_APPEND) for flags in opened), "no append open after a failed load"


def test_short_writes_are_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review item 3b: os.write may return a short count. Every byte still lands."""
    root = tmp_path / "t"
    real_write = os.write

    def short(fd: int, data: Any) -> int:
        return real_write(fd, bytes(data[:7]))

    monkeypatch.setattr(rt.os, "write", short)
    _writer(root).append_batch([_rec(i, "payload") for i in range(1, 6)], last_seq=5)
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2, 3, 4, 5]


def test_failed_write_leaves_no_torn_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review item 3b: a write that fails part way truncates back to the pre-batch size."""
    root = tmp_path / "t"
    writer = _writer(root)
    writer.append_batch([_rec(1), _rec(2)], last_seq=2)
    before = _file(root).stat().st_size
    real_write = os.write
    calls = {"n": 0}

    def fail_midway(fd: int, data: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, bytes(data[: len(data) // 2]))
        message = "disk full"
        raise OSError(errno.ENOSPC, message)

    monkeypatch.setattr(rt.os, "write", fail_midway)
    assert writer.append_batch([_rec(3), _rec(4)], last_seq=4) == 0
    assert _file(root).stat().st_size == before
    assert [r["seq"] for r in _lines(_file(root))] == [1, 2]


def test_a_record_that_cannot_be_recorded_becomes_a_marker_and_the_run_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon side of review item 1: one bad record writes a marker and the rest go on."""
    root = tmp_path / "t"
    real_to_line = rt._to_line

    def explode_on_seq_one(record: dict[str, Any]) -> bytes:
        if record.get("seq") == 1 and record.get("type") != "error":
            message = "cannot encode"
            raise ValueError(message)
        return real_to_line(record)

    monkeypatch.setattr(rt, "_to_line", explode_on_seq_one)
    _writer(root).append_batch([_rec(1), _rec(2)], last_seq=2)
    records = _lines(_file(root))
    assert [r["seq"] for r in records] == [1, 2]
    assert records[0]["type"] == "error"
    assert records[1]["type"] == "assistant_text"


# --- finding 3 (service): every awaited transcript operation is bounded --------------


class _FakeClient:
    def __init__(self, records: list[dict[str, Any]], last_seq: int | None) -> None:
        self.records = records
        self.last_seq = last_seq
        self.calls: list[dict[str, Any]] = []

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
        self.calls.append({"transcript_after": transcript_after})
        batch: list[dict[str, Any]] = []
        if transcript_after is not None:
            batch = [r for r in self.records if r["seq"] > transcript_after][:transcript_limit]
        return JobStatus(
            job_id=job_id,
            state="running",
            started_at=datetime.now(UTC),
            transcript=batch,
            transcript_last_seq=self.last_seq,
        )

    async def post_reset(self) -> SimpleNamespace:
        return SimpleNamespace(is_success=True, status_code=200)


def _service(client: _FakeClient, collector: TranscriptCollector | None) -> HTTPPerformerService:
    config = PerformerEndpointConfig(
        id="perf-review3",
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


async def test_a_slow_write_does_not_hold_up_the_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 3a / 4: one transcript write may not delay the status poll."""
    monkeypatch.setattr(hps, "_TRANSCRIPT_POLL_SECONDS", 0.1)
    monkeypatch.setattr(
        RunTranscriptWriter,
        "append_batch",
        lambda self, *a, **k: time.sleep(3) or 0,
    )
    client = _FakeClient([_rec(1)], last_seq=1)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    started = time.monotonic()
    with capture_logs() as logs:
        status = await service.check_status(
            "sess-slow",
            payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_s"}},
        )
    assert time.monotonic() - started < 2
    assert status["status"] == "working"
    assert any(entry["event"] == "run_transcript.poll_timeout" for entry in logs)


async def test_a_slow_first_cursor_read_falls_back_to_a_plain_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 3a: until the cursor is known, the poll asks for no records. Asking
    from a placeholder 0 would re-pull the run."""
    monkeypatch.setattr(hps, "_TRANSCRIPT_POLL_SECONDS", 0.1)
    monkeypatch.setattr(RunTranscriptWriter, "ensure_loaded", lambda self: time.sleep(3))
    client = _FakeClient([_rec(1)], last_seq=1)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    await service.check_status(
        "sess-load",
        payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_l"}},
    )
    assert client.calls[0]["transcript_after"] is None


async def test_terminal_status_is_returned_after_the_drain_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 reverses round 3's background finish: the tail is drained before the
    terminal state is reported, so no new dispatch can replace the job first."""
    client = _FakeClient([_rec(1)], last_seq=1)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    drained: list[str] = []

    async def drain(self: HTTPPerformerService, *args: Any, **kwargs: Any) -> None:
        drained.append("done")

    monkeypatch.setattr(HTTPPerformerService, "_drain_transcript", drain)

    async def terminal_get(job_id: str, **kwargs: Any) -> JobStatus:
        return JobStatus(
            job_id=job_id,
            state="succeeded",
            started_at=datetime.now(UTC),
            transcript=[_rec(1)],
            transcript_last_seq=2,
        )

    monkeypatch.setattr(client, "get_job", terminal_get)
    status = await service.check_status(
        "sess-term",
        payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_t"}},
    )
    assert drained == ["done"], "the drain finished before the terminal state was returned"
    assert "status" in status


# --- finding 5: over-redaction of code and descriptions ------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "def run(token: str)",
        "if token == x",
        '"token_count": 5',
        "token=5",
        '"token": 5',
        "def f(password: Optional[str])",
        "token_type = 'bearer'",
    ],
)
def test_code_and_counts_are_not_redacted(code: str) -> None:
    assert redact_text(code) == code


def test_a_quoted_pem_marker_in_prose_does_not_eat_the_text_after_it() -> None:
    prose = (
        "The parser looks for -----BEGIN RSA PRIVATE KEY----- in quotes.\n"
        "Then the normal text continues on the next line."
    )
    assert redact_text(prose) == prose


def test_a_pem_block_is_redacted_only_from_begin_to_end() -> None:
    text = (
        "before\n-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8w\n"
        "-----END PRIVATE KEY-----\nafter"
    )
    out = redact_text(text)
    assert "MIIBVQ" not in out
    assert out.startswith("before\n")
    assert out.endswith("\nafter")


def test_a_begin_without_end_redacts_only_when_base64_lines_follow() -> None:
    body = (
        "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8w\nZZZZYYYYXXXXWWWWVVVV\n"
    )
    assert "MIIBVQ" not in redact_text(body)
    prose = "-----BEGIN PRIVATE KEY----- is a marker\nand this is ordinary prose"
    assert redact_text(prose) == prose


# --- finding 6: redaction gaps ------------------------------------------------------


def test_netrc_password_is_redacted() -> None:
    out = redact_text("machine github.com login bob password s3cret-netrc-pw")
    assert "s3cret-netrc-pw" not in out


def test_bare_netrc_password_line_is_redacted_and_prose_is_not() -> None:
    assert "hunter-line" not in redact_text("password hunter-line")
    assert redact_text("Password is wrong") == "Password is wrong"


def test_bare_jwt_is_redacted() -> None:
    assert JWT not in redact_text(f"Authorization token {JWT} here")


def test_curl_user_password_is_redacted() -> None:
    out = redact_text("curl -u deploy:Hunter2Curl https://example.com")
    assert "Hunter2Curl" not in out
    assert "deploy:" in out


def test_ansi_sequences_between_key_and_value_do_not_hide_the_secret() -> None:
    out = redact_text("password\x1b[0m=hunter22abc")
    assert "hunter22abc" not in out


def test_structured_key_variants_are_masked() -> None:
    value = redact_value(
        {
            "pass_word": "variant-one-1234",
            "passphrase": "variant-two-1234",
            "api_key": "variant-three-1234",
            "apikey": "variant-four-1234",
            "OPENAI_API_KEY": "variant-five-1234",
        },
    )
    blob = json.dumps(value)
    for secret in ("variant-one", "variant-two", "variant-three", "variant-four", "variant-five"):
        assert secret not in blob


def test_stripe_live_key_is_redacted() -> None:
    key = "sk_live_" + "A" * 24
    assert key not in redact_text(f"charge with {key} now")


def test_pgp_private_key_block_is_redacted() -> None:
    block = (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBGabcdefghijklmnopqrstuv\n"
        "-----END PGP PRIVATE KEY BLOCK-----"
    )
    assert "lQOYBG" not in redact_text(block)


def test_descriptive_suffixes_are_not_secrets() -> None:
    assert tr.is_sensitive_key("token_count") is False
    assert tr.is_sensitive_key("GITHUB_TOKEN") is True


# --- finding 6: linearity, rerun ---------------------------------------------------


def test_five_megabyte_unbroken_word_redacts_in_well_under_two_seconds() -> None:
    text = "a" * 5_000_000
    started = time.perf_counter()
    assert redact_text(text) == text
    assert time.perf_counter() - started < 2.0


# --- finding 7: retention races and the shutdown path -------------------------------


def _age(path: Path, days: int) -> None:
    old = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (old, old))


def test_a_card_folder_racing_a_writer_does_not_abort_the_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 7: rmdir hits ENOTEMPTY because a writer just created a file there."""
    root = tmp_path / "t"
    expired = transcript_path(root, symphony="s", card_id="c1", run_id="old")
    expired.parent.mkdir(parents=True)
    expired.write_text('{"seq":1}\n', encoding="utf-8")
    _age(expired, days=45)
    other = transcript_path(root, symphony="s", card_id="c2", run_id="ok")
    other.parent.mkdir(parents=True)
    other.write_text('{"seq":1}\n', encoding="utf-8")
    _age(other, days=45)
    real_rmdir = Path.rmdir

    def racing_rmdir(self: Path) -> None:
        if self.name == "c1":
            raise OSError(errno.ENOTEMPTY, "racing writer")
        real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", racing_rmdir)
    removed = prune_expired(root, retention_days=30)
    assert removed == 2
    assert not expired.exists()
    assert not other.exists()


def test_the_sweep_skips_files_within_the_minimum_age(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 7: a file that is still young is never removed by the startup sweep."""
    root = tmp_path / "t"
    path = transcript_path(root, symphony="s", card_id="c", run_id="young")
    path.parent.mkdir(parents=True)
    path.write_text('{"seq":1}\n', encoding="utf-8")
    _age(path, days=45)
    monkeypatch.setattr(rt, "MIN_RETENTION_AGE_SECONDS", 10**9)
    assert prune_expired(root, retention_days=30) == 0
    assert path.exists()


async def test_the_retention_loop_exits_on_shutdown() -> None:
    """Review item 7: the sweep ends when the daemon stops, not only when cancelled."""
    stop = asyncio.Event()
    fake = SimpleNamespace(_stop_event=stop)
    collector = SimpleNamespace(root=Path("/nonexistent-transcripts-root"), retention_days=30)
    loop = CoordinareDaemon._transcript_retention_loop(
        cast("CoordinareDaemon", fake),
        cast("TranscriptCollector", collector),
    )
    task = asyncio.create_task(loop)
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


# --- finding 8: directories, names, ordering, releases ------------------------------


def test_new_directories_have_no_group_or_other_access(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _writer(root).append_batch([_rec(1)], last_seq=1)
    for folder in (root, root / "alpha", root / "alpha" / "PVTI_1"):
        assert stat.S_IMODE(folder.stat().st_mode) & 0o077 == 0, folder


def test_distinct_ids_never_share_a_file_name() -> None:
    assert sanitize_segment("a/b") != sanitize_segment("a_b")
    assert sanitize_segment("a/b").startswith("a_b-")
    assert sanitize_segment("PVTI_lADO-1.2") == "PVTI_lADO-1.2"


def test_a_lone_surrogate_is_written_not_dropped(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _writer(root).append_batch(
        [{"ts": "x", "run_id": "r", "seq": 1, "type": "assistant_text", "text": "bad \ud800 char"}],
        last_seq=1,
    )
    [record] = _lines(_file(root))
    assert record["seq"] == 1
    assert "�" in record["text"]


def test_a_released_run_has_no_secrets_left_in_the_service(tmp_path: Path) -> None:
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(_FakeClient([], last_seq=None), collector)
    service._transcript_secrets["sess-x"] = ["plaintext-secret-value"]
    service._release_transcript_run("sess-x")
    assert "sess-x" not in service._transcript_secrets


def test_transcript_path_for_distinct_run_ids_differs(tmp_path: Path) -> None:
    a = transcript_path(tmp_path, symphony="s", card_id="c", run_id="a/b")
    b = transcript_path(tmp_path, symphony="s", card_id="c", run_id="a_b")
    assert a != b

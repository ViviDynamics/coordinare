"""Spec 129, fourth review: one test per finding, each on the failing input.

Covers linear redaction on the four adversarial inputs (1 MB each), the relief
rules and the leaks they must not cause, the inline terminal drain with a
truncation marker for a tail that does not fit, the single drain per run, the
cursor and parse guards, and the retention and terminal-escape hardening.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from coordinare.lib import transcript_redaction as tr
from coordinare.lib.transcript_redaction import redact_text, redact_value
from coordinare.models.performer_endpoint import JobStatus, PerformerEndpointConfig
from coordinare.services import http_performer_service as hps
from coordinare.services import run_transcript as rt
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_transcript import (
    RunTranscriptWriter,
    TranscriptCollector,
    prune_expired,
    transcript_path,
)
from coordinare.transport.http_transport import PerformerHTTPClient

MEGABYTE = 1_000_000
# CPU bounds: linear runs take well under a second locally; quadratic ones took
# minutes. The margin is for slow shared CI runners.
GENEROUS_SECONDS = 8.0


def _timed(text: str) -> float:
    started = time.perf_counter()
    redact_text(text)
    return time.perf_counter() - started


# --- item 1: linear on the four adversarial inputs, 1 MB each ----------------------


def test_eyj_run_of_one_megabyte_is_linear() -> None:
    """The JWT scan used to restart at every ``eyJ``: 300 KB took 37 s."""
    assert _timed("eyJ" * (MEGABYTE // 3)) < GENEROUS_SECONDS


def test_machine_repeats_of_one_megabyte_are_linear() -> None:
    """The netrc group used to restart at every segment: 300 KB took 45 s."""
    assert _timed("machine a " * (MEGABYTE // 10)) < GENEROUS_SECONDS


def test_begin_repeated_on_one_line_of_one_megabyte_is_linear() -> None:
    """Each BEGIN used to rescan the rest of its line: 300 KB took over 60 s."""
    assert _timed("-----BEGIN PRIVATE KEY-----" * (MEGABYTE // 27)) < GENEROUS_SECONDS


def test_twenty_thousand_distinct_pem_kinds_are_linear() -> None:
    """Each distinct kind used to rescan for its END marker."""
    lines = []
    for index in range(20_000):
        kind = "".join(chr(65 + (index // 26**k) % 26) for k in range(4))
        lines.append(f"-----BEGIN {kind} PRIVATE KEY-----")
    assert _timed("\n".join(lines)) < GENEROUS_SECONDS


def test_prefix_run_of_one_megabyte_is_linear() -> None:
    """``sk-`` repeated shares one token; each start used to rescan it."""
    assert _timed("sk-" * (MEGABYTE // 3)) < GENEROUS_SECONDS


# Five times the input may take at most this many times as long. Linear work
# scales by about 5; the quadratic code this guards against scaled by about 25.
LINEAR_SCALE_LIMIT = 10.0


def test_five_megabyte_adversarial_runs_scale_linearly() -> None:
    """Measured as a ratio, so a slow runner or coverage tracing cannot fail it."""
    for unit, width in (("eyJ", 3), ("machine a ", 10)):
        one = _timed(unit * (MEGABYTE // width))
        five = _timed(unit * (5 * MEGABYTE // width))
        assert five < LINEAR_SCALE_LIMIT * max(one, 0.01), (unit, one, five)


# --- item 2: the relief rules do not leak -----------------------------------------

LEAKS: list[tuple[str, str]] = [
    ('password: str = "hunter2hunter2"', "hunter2hunter2"),
    ("password: str = hunter2hunter2", "hunter2hunter2"),
    ('token == "hunter2hunter2"', "hunter2hunter2"),
    ("API_KEY: 12345678", "12345678"),
    ("API_KEY=12345678", "12345678"),
    ("PASSWORD=12345678", "12345678"),
    ("pin=12345678", "12345678"),
    ("PASSWORD=true", "PASSWORD=true"),
    ("db_password_name=hunter2hunter2", "hunter2hunter2"),
    ("db_password_limit=hunter2hunter2", "hunter2hunter2"),
]


@pytest.mark.parametrize(("example", "secret"), LEAKS)
def test_relief_rules_do_not_leak(example: str, secret: str) -> None:
    out = redact_text(example)
    assert secret not in out, out


def test_numeric_structured_values_under_a_credential_key_are_redacted() -> None:
    assert redact_value({"password": 12345678}) == {"password": "[REDACTED]"}


def test_short_numbers_and_descriptive_names_are_kept() -> None:
    assert redact_value({"token_count": 5, "max_tokens": 4096}) == {
        "token_count": 5,
        "max_tokens": 4096,
    }
    assert redact_text("token_limit=9999999") == "token_limit=9999999"


def test_an_annotation_with_no_assignment_is_code() -> None:
    assert redact_text("def run(token: str)") == "def run(token: str)"
    assert redact_text("def run(token: str, other: int)") == "def run(token: str, other: int)"


def test_a_comparison_with_an_identifier_is_code() -> None:
    assert redact_text("if token == expected_value") == "if token == expected_value"


def test_round_three_over_redaction_cases_still_hold() -> None:
    for code in ("def run(token: str)", "if token == x", '"token_count": 5', "token=5"):
        assert redact_text(code) == code


# --- item 6 and 7: escapes and parse guards --------------------------------------


def test_osc_sequence_does_not_hide_a_secret() -> None:
    out = redact_text("\x1b]0;window title\x07password=hunter22abc")
    assert "hunter22abc" not in out


def test_osc_sequence_with_st_terminator_does_not_hide_a_secret() -> None:
    out = redact_text("\x1b]8;;http://x\x1b\\password=hunter23abc")
    assert "hunter23abc" not in out


def test_a_deeply_nested_tail_line_never_raises_out_of_the_parser() -> None:
    """Item 7: a RecursionError from json.loads is contained. Depth the C scanner accepts
    is a valid record shape, so only the absence of an exception is asserted."""
    rt._parse_line(b"[" * 5000 + b"]" * 5000)
    rt._parse_line(b"[" * 200_000)


def test_retention_survives_an_entry_whose_stat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 7: every metadata read in the sweep is inside the guard.

    Both ``stat`` and ``lstat`` fail for the bad entry, since which one a sweep
    calls (and which one ``Path`` uses underneath) differs between Python
    versions. CI on Linux showed the difference: ``is_file`` raised there.
    """
    root = tmp_path / "t"
    bad = transcript_path(root, symphony="s", card_id="c", run_id="bad")
    good = transcript_path(root, symphony="s", card_id="c", run_id="good")
    for path in (bad, good):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"seq":1}\n', encoding="utf-8")
        old = datetime.now(UTC).timestamp() - 90 * 86400
        os.utime(path, (old, old))
    # The sweep stats each entry by name relative to its folder descriptor (220), so the
    # transient failure is injected at os.stat for that name.
    real_os_stat = os.stat

    def failing_os_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == "bad.jsonl":
            message = "transient"
            raise OSError(message)
        return real_os_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing_os_stat)
    assert prune_expired(root, retention_days=30) == 1
    # os.path, not Path: on Python 3.12 Path.exists goes through the patched stat.
    assert os.path.exists(bad)
    assert not os.path.exists(good)


# --- item 6: an unreadable tail fails the run, it never resets the cursor -----------


def test_more_than_a_thousand_unparsable_lines_fail_the_writer_without_a_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "t"
    path = transcript_path(root, symphony="s", card_id="c", run_id="run-1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a record\n" * 1100)
    writer = RunTranscriptWriter(
        root,
        parts=("s", "c"),
        run_id="run-1",
        max_bytes=10_000_000,
    )
    writer.ensure_loaded()
    assert writer.failed is True
    assert writer.cursor == 0
    before = path.read_bytes()
    assert (
        writer.append_batch(
            [{"ts": "x", "run_id": "r", "seq": 1, "type": "assistant_text", "text": "hi"}],
            last_seq=1,
        )
        == 0
    )
    assert path.read_bytes() == before


# --- item 4: the tail beyond the bound is marked, not lost silently ------------------


def test_record_tail_lost_writes_one_truncated_marker_after_the_last_written_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "t"
    writer = RunTranscriptWriter(root, parts=("s", "c"), run_id="run-1", max_bytes=10_000_000)
    writer.append_batch(
        [{"ts": "x", "run_id": "r", "seq": 1, "type": "assistant_text", "text": "hi"}],
        last_seq=1,
    )
    writer.observe_last_seq(5)
    writer.record_tail_lost("drain timed out")
    writer.record_tail_lost("again")
    lines = [json.loads(line) for line in writer.path.read_text(encoding="utf-8").splitlines()]
    assert [line["type"] for line in lines] == ["assistant_text", "truncated"]
    assert lines[1]["seq"] == 2
    assert "not retrieved" in lines[1]["text"]


class _Client:
    def __init__(self, records: list[dict[str, Any]], last_seq: int | None) -> None:
        self.records = records
        self.last_seq = last_seq
        self.calls = 0

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
        self.calls += 1
        batch = [
            r for r in self.records if transcript_after is not None and r["seq"] > transcript_after
        ]
        return JobStatus(
            job_id=job_id,
            state="succeeded",
            started_at=datetime.now(UTC),
            transcript=batch[:transcript_limit],
            transcript_last_seq=self.last_seq,
        )

    async def post_reset(self) -> SimpleNamespace:
        return SimpleNamespace(is_success=True, status_code=200)


def _rec(seq: int) -> dict[str, Any]:
    return {"ts": "x", "run_id": "r", "seq": seq, "type": "assistant_text", "text": f"m{seq}"}


def _service(client: _Client, collector: TranscriptCollector) -> HTTPPerformerService:
    config = PerformerEndpointConfig(
        id="perf-review4",
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


async def test_terminal_drain_completes_before_the_terminal_status_is_returned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3 and 4: the tail is taken inline, before the terminal state is reported."""
    client = _Client([_rec(1), _rec(2), _rec(3)], last_seq=3)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    await service.check_status(
        "sess-inline",
        payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_i"}},
    )
    status = await service.check_status("sess-inline", payload={})
    assert "status" in status
    path = transcript_path(tmp_path / "t", symphony="alpha", card_id="PVTI_i", run_id="sess-inline")
    assert [json.loads(line)["seq"] for line in path.read_text(encoding="utf-8").splitlines()] == [
        1,
        2,
        3,
    ]


async def test_a_tail_that_does_not_fit_the_bound_is_marked_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4: a drain that cannot finish within its bound leaves a truncation marker."""
    monkeypatch.setattr(hps, "_TRANSCRIPT_DRAIN_SECONDS", 0.2)
    monkeypatch.setattr(hps, "_TRANSCRIPT_POLL_SECONDS", 0.5)

    class _Slow(_Client):
        async def get_job(self, job_id: str, **kwargs: Any) -> JobStatus:
            import asyncio

            await asyncio.sleep(0.05)
            after = int(kwargs.get("transcript_after") or 0)
            return JobStatus(
                job_id=job_id,
                state="succeeded",
                started_at=datetime.now(UTC),
                transcript=[_rec(after + 1)],
                transcript_last_seq=10**6,
            )

    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    client = _Slow([], last_seq=None)
    service = _service(cast("_Client", client), collector)
    writer = collector.writer_for(run_id="sess-tail", symphony="alpha", card_id="PVTI_t")
    terminal = JobStatus(
        job_id="sess-tail",
        state="succeeded",
        started_at=datetime.now(UTC),
        transcript=[],
        transcript_last_seq=10**6,
    )
    started = time.monotonic()
    await service._drain_transcript(
        cast("PerformerHTTPClient", client),
        "sess-tail",
        writer,
        terminal,
        timeout=0.2,
    )
    assert time.monotonic() - started < 3
    path = transcript_path(root, symphony="alpha", card_id="PVTI_t", run_id="sess-tail")
    assert any(
        json.loads(line)["type"] == "truncated"
        for line in path.read_text(encoding="utf-8").splitlines()
    )


async def test_a_run_is_drained_once_on_the_terminal_path_and_not_again_on_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 5: one drain and one teardown per run, on every path."""
    client = _Client([_rec(1)], last_seq=1)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    drains: list[str] = []
    real_drain = HTTPPerformerService._drain_transcript

    async def counting(self: HTTPPerformerService, *args: Any, **kwargs: Any) -> None:
        drains.append("drain")
        await real_drain(self, *args, **kwargs)

    monkeypatch.setattr(HTTPPerformerService, "_drain_transcript", counting)
    await service.check_status(
        "sess-once",
        payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_o"}},
    )
    await service.check_status("sess-once", payload={})
    await service.release_session("sess-once")
    assert len(drains) == 1


def test_pin_is_a_bare_number_stem_and_not_a_key_stem() -> None:
    """``pin`` is redacted only as a bare number, so ``pin=1234`` is a credential while
    the key itself is not a secret-holding name."""
    assert redact_text("pin=1234") == "pin=[REDACTED]"
    assert tr.is_sensitive_key("pin") is False

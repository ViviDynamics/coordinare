"""Spec 129, fifth review: one test per blocker and follow-up, each on the failing input.

Covers a malformed transcript field never failing the status poll (blocker 1),
teardown that survives an outer cancellation (blocker 2), no retained job secrets
after a failed dispatch (blocker 3), the drain bound (item 4), the retry and the
truncation marker for a failed drain (item 5), finalisation only after a confirmed
stop (item 6), the redaction relief and new patterns (items 7 and 8).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from structlog.testing import capture_logs

from coordinare.lib.transcript_redaction import redact_text
from coordinare.models.performer_endpoint import JobStatus, PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService, _EphemeralJob
from coordinare.services.run_transcript import TranscriptCollector, transcript_path
from coordinare.transport.http_transport import PerformerHTTPClient
from coordinare.workspace import WorkspaceInfo

MEGABYTE = 1_000_000
# CPU bounds: linear runs take well under a second locally; quadratic ones took
# minutes. The margin is for slow shared CI runners.
GENEROUS_SECONDS = 8.0


def _persistent_config() -> PerformerEndpointConfig:
    return PerformerEndpointConfig.model_validate(
        {
            "id": "perf-r5",
            "mode": "persistent",
            "roles": ["implementing"],
            "image": "performer:base",
            "endpoint": "http://127.0.0.1:8080",
        },
    )


def _ephemeral_config() -> PerformerEndpointConfig:
    return PerformerEndpointConfig.model_validate(
        {
            "id": "perf-r5-e",
            "mode": "ephemeral",
            "roles": ["implementing"],
            "image": "performer:base",
        },
    )


def _workspace() -> WorkspaceInfo:
    return WorkspaceInfo(
        path=None,
        repo_url="https://github.com/x/y",
        branch="main",
        github_token="ghs_fifthreviewtoken",
    )


def _card() -> dict[str, object]:
    return {
        "id": "card-r5",
        "role": "implementer",
        "backend": "claude_code",
        "persona_instructions": "be excellent",
    }


def _rec(seq: int) -> dict[str, Any]:
    return {"ts": "x", "run_id": "r", "seq": seq, "type": "assistant_text", "text": f"m{seq}"}


def _terminal(job_id: str, last_seq: int | None) -> JobStatus:
    return JobStatus(
        job_id=job_id,
        state="succeeded",
        started_at=datetime.now(UTC),
        transcript=[],
        transcript_last_seq=last_seq,
    )


# --- blocker 1: a malformed transcript field never fails the status poll ----------

BASE_BODY: dict[str, Any] = {
    "job_id": "sess-bad",
    "state": "succeeded",
    "started_at": "2026-01-01T00:00:00Z",
    "transcript": [],
    "transcript_last_seq": 0,
}

MALFORMED: list[tuple[str, dict[str, Any]]] = [
    ("transcript_is_a_string", {"transcript": "x"}),
    ("last_seq_is_a_string", {"transcript_last_seq": "x"}),
    ("error_is_an_object", {"transcript_error": {"kind": "boom"}}),
    ("transcript_holds_a_string", {"transcript": ["x"]}),
    ("transcript_is_null", {"transcript": None}),
]


@pytest.mark.parametrize(("label", "override"), MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_a_malformed_transcript_field_never_fails_the_status(
    label: str,
    override: dict[str, Any],
    tmp_path: Path,
) -> None:
    body = {**BASE_BODY, **override}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={})

    client = PerformerHTTPClient(
        "http://127.0.0.1:8080",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    svc = HTTPPerformerService(_persistent_config(), client=client, transcripts=collector)
    with capture_logs() as logs:
        result = await svc.check_status(
            "sess-bad",
            payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_m"}},
        )
    assert isinstance(result, dict), label
    assert "sess-bad" in svc._transcript_finalised, label
    assert any(entry["event"] == "run_transcript.performer_batch_error" for entry in logs), label


@pytest.mark.parametrize(("label", "override"), MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_a_malformed_transcript_field_is_read_leniently_by_the_transport(
    label: str,
    override: dict[str, Any],
) -> None:
    body = {**BASE_BODY, **override}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = PerformerHTTPClient(
        "http://127.0.0.1:8080",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    status = await client.get_job("sess-bad", transcript_after=0)
    assert status.state == "succeeded", label
    assert status.transcript == [], label
    assert status.transcript_last_seq is None or isinstance(status.transcript_last_seq, int)
    assert status.transcript_error, label


# --- blocker 2: teardown survives an outer cancellation ------------------------------


class _HangingClient:
    """Answers the first call when asked, then hangs on every transcript pull."""

    def __init__(self, *, answer_first: bool, answer_always: bool = False) -> None:
        self.answer_first = answer_first
        self.answer_always = answer_always
        self.calls = 0
        self.hung = asyncio.Event()

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
        self.calls += 1
        if self.answer_always or (self.answer_first and self.calls == 1):
            return _terminal(job_id, last_seq=5)
        self.hung.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        return None


class _Runtime:
    def __init__(self, *, still_running: bool = False) -> None:
        self.stopped: list[str] = []
        self.still_running = still_running

    async def stop(self, handle: str, **_kw: Any) -> None:
        self.stopped.append(handle)

    async def find_session(self, session_id: str) -> object | None:
        return SimpleNamespace(handle="pod-c") if self.still_running else None


def _ephemeral_service(
    tmp_path: Path,
    client: _HangingClient,
    runtime: _Runtime,
) -> HTTPPerformerService:
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    svc = HTTPPerformerService(_ephemeral_config(), runtime=runtime, transcripts=collector)
    job = _EphemeralJob(
        container_id="pod-c",
        endpoint="http://127.0.0.1:9",
        client=cast("PerformerHTTPClient", client),
    )
    job.job_id = "job-c"
    svc._active_jobs["sess-c"] = job
    return svc


async def _cancel_during_drain(
    svc: HTTPPerformerService,
    client: _HangingClient,
    start: Any,
) -> None:
    task = asyncio.ensure_future(start(svc))
    await asyncio.wait_for(client.hung.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _assert_torn_down(svc: HTTPPerformerService, runtime: _Runtime) -> None:
    assert runtime.stopped == ["pod-c"]
    assert "sess-c" not in svc._active_jobs
    assert "sess-c" in svc._transcript_finalised
    assert "sess-c" not in svc._transcript_secrets


async def test_cancel_during_terminal_drain_still_tears_down(tmp_path: Path) -> None:
    client = _HangingClient(answer_first=True)
    runtime = _Runtime()
    svc = _ephemeral_service(tmp_path, client, runtime)
    await _cancel_during_drain(
        svc,
        client,
        lambda s: s.check_status("sess-c", payload={}),
    )
    _assert_torn_down(svc, runtime)


async def test_cancel_during_release_drain_still_tears_down(tmp_path: Path) -> None:
    client = _HangingClient(answer_first=False)
    runtime = _Runtime()
    svc = _ephemeral_service(tmp_path, client, runtime)
    await _cancel_during_drain(svc, client, lambda s: s.release_session("sess-c"))
    _assert_torn_down(svc, runtime)


async def test_cancel_during_cleanup_drain_still_tears_down(tmp_path: Path) -> None:
    client = _HangingClient(answer_first=False)
    runtime = _Runtime()
    svc = _ephemeral_service(tmp_path, client, runtime)
    await _cancel_during_drain(
        svc,
        client,
        lambda s: s._cleanup_ephemeral_job_by_id("sess-c"),
    )
    _assert_torn_down(svc, runtime)


async def test_cancel_during_stop_drain_still_stops_once(tmp_path: Path) -> None:
    client = _HangingClient(answer_first=False)
    runtime = _Runtime()
    svc = _ephemeral_service(tmp_path, client, runtime)
    await _cancel_during_drain(svc, client, lambda s: s.stop_session_confirmed("sess-c"))
    _assert_torn_down(svc, runtime)


# --- item 6: finalise only after a confirmed stop -----------------------------------


async def test_an_unconfirmed_stop_does_not_finalise_the_transcript(tmp_path: Path) -> None:
    client = _HangingClient(answer_first=False, answer_always=True)
    runtime = _Runtime(still_running=True)
    svc = _ephemeral_service(tmp_path, client, runtime)
    assert await svc.stop_session_confirmed("sess-c") is False
    assert "sess-c" not in svc._transcript_finalised
    assert svc._transcripts is not None
    assert svc._transcript_writer("sess-c", None) is not None


async def test_a_confirmed_stop_finalises_the_transcript(tmp_path: Path) -> None:
    client = _HangingClient(answer_first=False, answer_always=True)
    runtime = _Runtime(still_running=False)
    svc = _ephemeral_service(tmp_path, client, runtime)
    assert await svc.stop_session_confirmed("sess-c") is True
    assert "sess-c" in svc._transcript_finalised


# --- blocker 3: no retained job secrets after a failed dispatch ---------------------


def _dispatch_service(tmp_path: Path, handler: Any) -> HTTPPerformerService:
    client = PerformerHTTPClient(
        "http://127.0.0.1:8080",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    return HTTPPerformerService(_persistent_config(), client=client, transcripts=collector)


async def test_five_busy_dispatches_leave_no_secret_entries(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"accepted": False, "reason": "busy"})

    svc = _dispatch_service(tmp_path, handler)
    for _ in range(5):
        result = await svc.dispatch_card(_card(), _workspace())
        assert result["status"] == "error"
    assert svc._transcript_secrets == {}


async def test_a_failed_post_leaves_no_secret_entries(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    svc = _dispatch_service(tmp_path, handler)
    result = await svc.dispatch_card(_card(), _workspace())
    assert result["status"] == "error"
    assert svc._transcript_secrets == {}


async def test_an_exception_from_post_job_leaves_no_secret_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    svc = _dispatch_service(tmp_path, handler)

    async def boom(self: PerformerHTTPClient, payload: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(PerformerHTTPClient, "post_job", boom)
    with pytest.raises(RuntimeError):
        await svc.dispatch_card(_card(), _workspace())
    assert svc._transcript_secrets == {}


async def test_an_accepted_dispatch_keeps_its_secrets_for_redaction(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"accepted": True, "job_id": "job-ok", "started_at": "2026-01-01T00:00:00Z"},
            )
        return httpx.Response(200, json={})

    svc = _dispatch_service(tmp_path, handler)
    await svc.dispatch_card(_card(), _workspace())
    assert any(
        "ghs_fifthreviewtoken" in value
        for values in svc._transcript_secrets.values()
        for value in values
    )


# --- item 4: the drain is bounded as a whole -------------------------------------------


async def test_the_drain_bound_includes_the_tail_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _HangingClient(answer_first=False)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    svc = HTTPPerformerService(
        _persistent_config(),
        client=cast("PerformerHTTPClient", client),
        transcripts=collector,
    )
    writer = collector.writer_for(run_id="sess-bound", symphony="alpha", card_id="PVTI_b")

    def slow_marker(reason: str) -> None:
        time.sleep(1.5)

    monkeypatch.setattr(writer, "record_tail_lost", slow_marker)
    started = time.monotonic()
    await svc._drain_transcript(
        cast("PerformerHTTPClient", client),
        "sess-bound",
        writer,
        _terminal("sess-bound", last_seq=10),
        timeout=0.6,
    )
    assert time.monotonic() - started < 1.5


# --- item 5: a failed drain retries once, then marks the tail lost --------------------


class _Flaky:
    def __init__(self, records: list[dict[str, Any]], last_seq: int, failures: int) -> None:
        self.records = records
        self.last_seq = last_seq
        self.failures = failures

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
        if self.failures > 0:
            self.failures -= 1
            raise httpx.ConnectError("reset")
        after = int(transcript_after or 0)
        batch = [r for r in self.records if r["seq"] > after]
        return JobStatus(
            job_id=job_id,
            state="succeeded",
            started_at=datetime.now(UTC),
            transcript=batch[:transcript_limit],
            transcript_last_seq=self.last_seq,
        )


def _writer_path(root: Path, run_id: str) -> Path:
    return transcript_path(root, symphony="alpha", card_id="PVTI_f", run_id=run_id)


async def test_one_failed_drain_fetch_is_retried_and_the_tail_is_written(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    svc = HTTPPerformerService(_persistent_config(), client=None, transcripts=collector)
    writer = collector.writer_for(run_id="sess-retry", symphony="alpha", card_id="PVTI_f")
    client = _Flaky([_rec(1), _rec(2)], last_seq=2, failures=1)
    await svc._drain_transcript(cast("PerformerHTTPClient", client), "sess-retry", writer, None)
    lines = _writer_path(root, "sess-retry").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [1, 2]
    assert writer.failed is False


async def test_a_tail_lost_to_failed_fetches_is_marked_truncated(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    svc = HTTPPerformerService(_persistent_config(), client=None, transcripts=collector)
    writer = collector.writer_for(run_id="sess-lost", symphony="alpha", card_id="PVTI_f")
    client = _Flaky([_rec(1), _rec(2)], last_seq=2, failures=10)
    # The terminal poll reported last_seq 2, so the writer knows records are missing.
    with capture_logs() as logs:
        await svc._drain_transcript(
            cast("PerformerHTTPClient", client),
            "sess-lost",
            writer,
            _terminal("sess-lost", last_seq=2),
        )
    lines = [
        json.loads(line)
        for line in _writer_path(root, "sess-lost").read_text(encoding="utf-8").splitlines()
    ]
    assert lines and lines[-1]["type"] == "truncated"
    assert writer.failed is False
    assert any(entry["event"] == "run_transcript.drain_tail_lost" for entry in logs)


# --- items 7 and 8: redaction relief and new patterns ---------------------------------

RELIEF: list[str] = [
    "self.token_cache = {}",
    "token_ttl: 3600",
    "token_expiry=3600",
    "token_timeout_ms=5000",
    "password_reset_sent: true",
    "credentials_file: path",
    "token: null",
    "bearer_auth = True",
]


@pytest.mark.parametrize("example", RELIEF)
def test_descriptive_names_and_non_secret_values_are_kept(example: str) -> None:
    assert redact_text(example) == example


STILL_REDACTED: list[tuple[str, str]] = [
    ("db_password_name=hunter2hunter2", "hunter2hunter2"),
    ("PASSWORD=true", "PASSWORD=true"),
    ("password_reset: hunter2hunter2", "hunter2hunter2"),
    ("authorization_header: Bearer hunter2hunter2", "hunter2hunter2"),
]


@pytest.mark.parametrize(("example", "secret"), STILL_REDACTED)
def test_strong_or_secret_valued_names_stay_redacted(example: str, secret: str) -> None:
    assert secret not in redact_text(example)


NEW_LEAKS: list[tuple[str, str]] = [
    ("curl --token hunter2hunter2 https://x", "hunter2hunter2"),
    ("tool --password hunter2hunter2", "hunter2hunter2"),
    ("redis://:hunter2hunter2@cache:6379/0", "hunter2hunter2"),
    ("mysql -uroot -phunter2hunter2 app", "hunter2hunter2"),
    ("password:\n  hunter2hunter2\nnext: 1", "hunter2hunter2"),
]


@pytest.mark.parametrize(("example", "secret"), NEW_LEAKS)
def test_new_redaction_patterns_do_not_leak(example: str, secret: str) -> None:
    out = redact_text(example)
    assert secret not in out, out


def test_a_space_separated_flag_is_not_redacted_as_a_flag_on_its_own() -> None:
    assert redact_text("tool --verbose --token --verbose") == "tool --verbose --token --verbose"


def test_new_patterns_are_linear_at_one_megabyte() -> None:
    samples = [
        "--token " * (MEGABYTE // 8),
        "mysql -p" * (MEGABYTE // 8),
        "redis://:" * (MEGABYTE // 8),
        "password:\n" * (MEGABYTE // 10),
    ]
    for sample in samples:
        started = time.perf_counter()
        redact_text(sample)
        assert time.perf_counter() - started < GENEROUS_SECONDS

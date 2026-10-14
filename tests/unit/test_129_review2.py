"""Spec 129, second review: one test per finding that exercises the failing input.

Covers linear redaction, the redaction rules, tail-only loading, off-loop writes,
hard links, the scoped retention sweep, root validation, dispatch-time mount
conflicts, release of per-run daemon state, and the finished-run guard.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from structlog.testing import capture_logs

from coordinare.lib import transcript_redaction as tr
from coordinare.lib.transcript_redaction import redact_text, redact_value
from coordinare.models.performer_endpoint import (
    JobInitPayload,
    JobStatus,
    PerformerEndpointConfig,
    VolumeMount,
)
from coordinare.services import run_transcript as rt
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_transcript import (
    RootGuard,
    RunTranscriptWriter,
    TranscriptCollector,
    make_collector,
    prune_expired,
    resolve_transcripts_root,
    transcript_path,
    transcript_root_conflicts,
)
from coordinare.transport.http_transport import PerformerHTTPClient

HUGE = 1_000_000


def _rec(seq: int, text: str = "message") -> dict[str, Any]:
    return {
        "ts": "2026-10-13T00:00:00+00:00",
        "run_id": "performer-side-id",
        "seq": seq,
        "type": "assistant_text",
        "text": f"{text} {seq}",
    }


def _file(root: Path) -> Path:
    return root / "alpha" / "PVTI_1" / "run-1.jsonl"


def _writer(root: Path) -> RunTranscriptWriter:
    return RunTranscriptWriter(
        root,
        parts=("alpha", "PVTI_1"),
        run_id="run-1",
        max_bytes=10_000_000,
    )


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# --- finding 1: linear redaction --------------------------------------------------


def test_one_megabyte_unbroken_alphanumeric_run_redacts_in_well_under_a_second() -> None:
    """HIGH 1: the old leading-class regex took 1.8 s at 8 KB and 31 s at 24 KB."""
    text = "a" * HUGE
    started = time.perf_counter()
    out = redact_text(text)
    assert time.perf_counter() - started < 1.0
    assert out == text


def test_long_runs_containing_key_stems_are_linear() -> None:
    started = time.perf_counter()
    redact_text("FOO_SECRET" * 100_000)
    redact_text('x="' * 200_000)
    redact_text("token=" * 200_000)
    assert time.perf_counter() - started < 1.0


# --- finding 5: redaction leaks and over-redaction ----------------------------------


def test_escaped_json_inside_a_string_is_redacted() -> None:
    """A JSON object carried inside a string keeps its quotes escaped."""
    text = r"{\"ANTHROPIC_AUTH_TOKEN\": \"hunter2hunter2\"}"
    out = redact_text(text)
    assert "hunter2hunter2" not in out
    assert "[REDACTED]" in out


def test_url_userinfo_password_is_redacted_and_the_host_kept() -> None:
    out = redact_text("clone https://deploy:Hunter2Pass@github.com/acme/x.git")
    assert "Hunter2Pass" not in out
    assert "github.com/acme/x.git" in out
    assert "deploy:" in out


def test_pem_private_key_block_is_redacted_whole() -> None:
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIBabcdef\nxyz123\n-----END RSA PRIVATE KEY----- tail"
    out = redact_text(text)
    assert "MIIBabcdef" not in out
    assert "xyz123" not in out
    assert "-----END" not in out
    assert out.endswith(" tail")


def test_unquoted_equals_value_is_redacted_to_end_of_line() -> None:
    out = redact_text("FOO_SECRET=hunter two words\nnext line")
    assert "hunter" not in out
    assert "two words" not in out
    assert out.endswith("next line")


def test_tokenizer_and_plural_tokens_are_not_treated_as_credentials() -> None:
    """Over-redaction: ``tokenizer`` and usage counts are not stems."""
    text = "tokenizer=abc and tokens: 12"
    assert redact_text(text) == text
    assert redact_value({"tokenizer": "keep-me", "input_tokens": 5, "max_tokens": 9}) == {
        "tokenizer": "keep-me",
        "input_tokens": 5,
        "max_tokens": 9,
    }


def test_prose_token_colon_redacts_one_word_by_design() -> None:
    """Accepted over/under-redaction: ``token: the cache is stale`` redacts ``the``.

    Redacting an unquoted ``:`` value to the end of the line would hide the value
    of ``Authorization: Basic xyz`` and ``token: abc def``. That would reopen a
    leak, so the single-token rule is kept on purpose (see the comment in
    ``_redact_assignments``).
    """
    assert redact_text("token: the cache is stale") == "token: [REDACTED] cache is stale"


def test_stems_are_whole_words_in_the_shared_module() -> None:
    assert tr.is_sensitive_key("GITHUB_TOKEN")
    assert tr.is_sensitive_key("api-key")
    assert not tr.is_sensitive_key("tokenizer")
    assert not tr.is_sensitive_key("passwordless")


# --- finding 2: tail-only load and off-loop writes ----------------------------------


def test_cursor_is_read_from_the_tail_not_the_whole_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 2: a multi-megabyte file must not be read whole on writer creation."""
    root = tmp_path / "t"
    path = _file(root)
    path.parent.mkdir(parents=True)
    count = 20_000
    with path.open("w") as handle:
        for seq in range(1, count + 1):
            handle.write(_with_seq(seq))
    size = path.stat().st_size
    assert size > 3_000_000

    read_total = [0]
    real_pread = os.pread

    def counting(fd: int, n: int, offset: int) -> bytes:
        data = real_pread(fd, n, offset)
        read_total[0] += len(data)
        return data

    monkeypatch.setattr(rt.os, "pread", counting)
    writer = _writer(root)
    assert writer.loaded is False, "construction does no I/O"
    writer.ensure_loaded()
    assert writer.cursor == count
    assert read_total[0] < 1_000_000, "only the last line and a chunk are read"


def _with_seq(seq: int) -> str:
    return (
        json.dumps(
            {
                "ts": "2026-10-13T00:00:00+00:00",
                "run_id": "r",
                "seq": seq,
                "type": "assistant_text",
                "text": "y" * 180,
            },
        )
        + "\n"
    )


async def test_batch_writes_run_on_a_worker_thread_not_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 2: the file and redaction work happen off the event loop."""
    threads: list[int] = []
    real_append = RunTranscriptWriter.append_batch

    def spy(self: RunTranscriptWriter, *args: Any, **kwargs: Any) -> int:
        threads.append(threading.get_ident())
        return real_append(self, *args, **kwargs)

    monkeypatch.setattr(RunTranscriptWriter, "append_batch", spy)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(_FakeClient(), collector)
    writer = _writer(tmp_path / "t")
    status = JobStatus(
        job_id="s",
        state="running",
        started_at=datetime.now(UTC),
        transcript=[_rec(1)],
        transcript_last_seq=1,
    )
    await service._absorb_transcript(writer, status, "s")
    assert threads, "the batch was written"
    assert threads[0] != threading.get_ident()


# --- finding 4: hard links ---------------------------------------------------------


def test_hard_link_inside_the_root_is_refused_and_the_outside_file_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "t"
    _file(root).parent.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"seq":1}\n')
    os.link(outside, _file(root))
    writer = _writer(root)
    assert writer.append_batch([_rec(2)], last_seq=2) == 0
    assert writer.failed is True
    assert outside.read_text() == '{"seq":1}\n'


def test_torn_line_is_not_trimmed_through_a_hard_link(tmp_path: Path) -> None:
    """Finding 4: the truncation on load is a write too, so the link check comes first."""
    root = tmp_path / "t"
    _file(root).parent.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b'{"seq":1}\n{"seq":2,"tor')
    os.link(outside, _file(root))
    writer = _writer(root)
    writer.ensure_loaded()
    assert writer.failed is True
    assert outside.read_bytes() == b'{"seq":1}\n{"seq":2,"tor'


# --- finding 3: scoped retention and validated roots --------------------------------


def _age(path: Path, days: int) -> None:
    old = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (old, old))


def test_retention_only_removes_the_writers_layout_and_live_files(tmp_path: Path) -> None:
    """Finding 3: a stray .jsonl at another depth, a badly named file and a live writer's
    file are never deleted. Only an old file in the exact layout is."""
    root = tmp_path / "t"
    layout = root / "alpha" / "PVTI_1"
    layout.mkdir(parents=True)
    expired = layout / "run-old.jsonl"
    live = layout / "run-live.jsonl"
    stray_top = root / "stray.jsonl"
    stray_deep = layout / "deeper" / "x.jsonl"
    stray_deep.parent.mkdir()
    bad_name = layout / ".hidden.jsonl"
    for path in (expired, live, stray_top, stray_deep, bad_name):
        path.write_text('{"seq":1}\n')
        _age(path, days=45)
    removed = prune_expired(root, retention_days=30, keep=frozenset({live}))
    assert removed == 1
    assert not expired.exists()
    assert live.exists()
    assert stray_top.exists()
    assert stray_deep.exists()
    assert bad_name.exists()


def test_expanduser_applies_to_the_transcripts_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = resolve_transcripts_root(
        Path("~/tx"),
        config_dir=tmp_path / "cfg",
        state_dir=tmp_path / "state",
    )
    assert root == tmp_path / "home" / "tx"


def test_root_equal_to_or_containing_a_protected_directory_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config = tmp_path / "cfg"
    attempts = config / "logs" / "attempts"
    guard = RootGuard(protected=(state, config, attempts))
    assert transcript_root_conflicts(config, guard)
    assert transcript_root_conflicts(attempts, guard)
    assert transcript_root_conflicts(tmp_path, guard), "a root that contains them is refused"
    assert transcript_root_conflicts(state / "transcripts", guard) == [], "the default is allowed"


def test_root_inside_an_env_cache_root_is_refused(tmp_path: Path) -> None:
    env_root = tmp_path / "env-caches"
    guard = RootGuard(performer_mounts=(env_root,))
    assert transcript_root_conflicts(env_root / "transcripts", guard)


def test_case_insensitive_filesystem_matches_names_case_insensitively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = tmp_path / "state"
    guard = RootGuard(protected=(protected,))
    variant = tmp_path / "State"
    # The probe is forced both ways, so the test does not depend on the host volume.
    monkeypatch.setattr(rt, "_case_insensitive", lambda _path: False)
    assert transcript_root_conflicts(variant, guard) == [], "case-sensitive: different directory"
    monkeypatch.setattr(rt, "_case_insensitive", lambda _path: True)
    assert transcript_root_conflicts(variant, guard), "case-insensitive: same directory"


def test_disabled_collector_builds_with_no_guard_check_and_no_disk_access(tmp_path: Path) -> None:
    root = tmp_path / "x" / "transcripts"
    collector = make_collector(
        enabled=False,
        root=root,
        max_bytes_per_run=10**6,
        retention_days=30,
        guard=RootGuard(performer_mounts=(tmp_path / "x",)),
    )
    assert collector.enabled is False
    assert not (tmp_path / "x").exists()


# --- finding 3 (dispatch time): per-run env-cache mounts ------------------------------


def _payload() -> JobInitPayload:
    return JobInitPayload(
        job_id="uuid-from-build",
        card_id="PVTI_1",
        role="implementer",
        backend="claude_code",
        persona="coder",
        repo_url="https://github.com/acme/widgets",
        branch="main",
    )


def test_dispatch_with_a_mount_inside_the_root_is_not_transcribed(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    service = _service(_FakeClient(), collector)
    mount = VolumeMount(
        host_path=root / "env-cache-for-symphony",
        container_path="/devenv/alpha",
        mode="ro",
    )
    payload = service._transcript_identity("sess-mount", _payload(), [mount])
    assert payload.transcript_max_bytes is None, "the performer gets no cap"
    assert service._transcript_writer("sess-mount", {}) is None


def test_disabled_transcripts_leave_the_payload_exactly_as_before(tmp_path: Path) -> None:
    """Finding 8: with transcripts off the payload is the pre-129 payload, unchanged."""
    collector = TranscriptCollector(tmp_path / "t", enabled=False, max_bytes_per_run=10**6)
    service = _service(_FakeClient(), collector)
    original = _payload()
    assert service._transcript_identity("sess-off", original, None) is original


def test_enabled_transcripts_use_the_session_id_as_the_job_id(tmp_path: Path) -> None:
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=4096)
    service = _service(_FakeClient(), collector)
    payload = service._transcript_identity("sess-on", _payload(), None)
    assert payload.job_id == "sess-on"
    assert payload.transcript_max_bytes == 4096


# --- finding 6 and 7: release and the finished-run guard ---------------------------


async def test_cleanup_releases_every_per_run_daemon_record(tmp_path: Path) -> None:
    """Finding 6: a system-error teardown leaves no secrets or markers behind."""
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(_FakeClient(), collector)
    service._transcript_secrets["sess-r"] = ["plaintext-secret-value"]
    service._transcript_targets["sess-r"] = ("alpha", "PVTI_1")
    service._transcript_failed_runs.add("sess-r")
    service._transcript_unsupported_runs.add("sess-r")
    service._transcript_refused.add("sess-r")
    service._transcript_writer(
        "sess-r",
        {"transcript_target": {"symphony": "alpha", "card_id": "PVTI_1"}},
    )
    await service._cleanup_ephemeral_job_by_id("sess-r")
    assert "sess-r" not in service._transcript_secrets
    assert "sess-r" not in service._transcript_targets
    assert "sess-r" not in service._transcript_failed_runs
    assert "sess-r" not in service._transcript_unsupported_runs
    assert "sess-r" not in service._transcript_refused
    assert "sess-r" not in collector._writers


async def test_finished_run_never_gets_a_writer_again(tmp_path: Path) -> None:
    """Finding 7: after release, a late poll or release creates nothing, least of all
    a target-less writer under unattributed/."""
    root = tmp_path / "t"
    client = _FakeClient(records=[_rec(1), _rec(2)], last_seq=2)
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    await service.check_status(
        "sess-done",
        payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_1"}},
    )
    await service.release_session("sess-done")
    client.records.append(_rec(3))
    client.last_seq = 3
    await service.check_status("sess-done", payload={})
    await service.release_session("sess-done")
    assert not (root / "unattributed").exists()
    assert "sess-done" not in collector._writers
    assert service._transcript_writer("sess-done", {}) is None


async def test_cursor_ahead_of_the_performer_is_logged(tmp_path: Path) -> None:
    """Finding 7: a cursor past the performer's last seq is reported, not ignored."""
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(_FakeClient(), collector)
    writer = collector.writer_for(run_id="sess-ahead", symphony="alpha", card_id="PVTI_1")
    writer.append_batch([_rec(i) for i in range(1, 6)], last_seq=5)
    status = JobStatus(
        job_id="sess-ahead",
        state="running",
        started_at=datetime.now(UTC),
        transcript=[],
        transcript_last_seq=3,
    )
    with capture_logs() as logs:
        await service._absorb_transcript(writer, status, "sess-ahead")
    assert any(entry["event"] == "run_transcript.cursor_ahead_of_performer" for entry in logs)


# --- helpers ---------------------------------------------------------------------


class _FakeClient:
    def __init__(
        self,
        records: list[dict[str, Any]] | None = None,
        *,
        last_seq: int | None = None,
    ) -> None:
        self.records = records or []
        self.last_seq = last_seq

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
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


def _service(client: _FakeClient, collector: TranscriptCollector | None) -> HTTPPerformerService:
    config = PerformerEndpointConfig(
        id="perf-review2",
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


def test_transcript_path_is_still_lexical(tmp_path: Path) -> None:
    """Guards the lexical-join contract the symlink refusal depends on."""
    path = transcript_path(tmp_path, symphony="a", card_id="b", run_id="c")
    assert path == tmp_path / "a" / "b" / "c.jsonl"

"""Spec 129: performer-side transcript buffer, redaction, cap and job-runner delivery."""
from __future__ import annotations

import asyncio

from performer import transcript
from performer.server.job_runner import JobRunner
from performer.server.models import JobInitPayload, JobResult

GH_TOKEN = "ghp_" + "a" * 36
BEARER = "Bearer " + "x" * 30


def _payload(job_id: str, max_bytes: int | None) -> JobInitPayload:
    return JobInitPayload(
        job_id=job_id,
        card_id="PVTI_card",
        role="implementer",
        backend="claude_code",
        persona="coder",
        repo_url="https://github.com/acme/widgets",
        branch="main",
        transcript_max_bytes=max_bytes,
    )


def test_emit_is_noop_without_active_buffer() -> None:
    transcript.set_active_buffer(None)
    assert transcript.transcript_enabled() is False
    transcript.emit("user", text="nothing recorded")


def test_seq_is_contiguous_and_cursor_pages_in_order() -> None:
    buf = transcript.TranscriptBuffer("run-1", max_bytes=10_000_000)
    for i in range(5):
        buf.append("assistant_text", text=f"msg {i}")
    assert buf.last_seq == 5
    first = buf.after(0, 2)
    assert [r["seq"] for r in first] == [1, 2]
    second = buf.after(2, 2)
    assert [r["seq"] for r in second] == [3, 4]
    assert [r["seq"] for r in buf.after(5, 10)] == []
    assert all(r["run_id"] == "run-1" for r in first)


def test_full_text_is_not_truncated() -> None:
    buf = transcript.TranscriptBuffer("run-1", max_bytes=10_000_000)
    long_text = "y" * 5000
    buf.append("tool_result", output_data=long_text, call_id="c1", is_error=False)
    [rec] = buf.after(0, 1)
    assert rec["output"] == long_text
    assert rec["call_id"] == "c1"
    assert rec["is_error"] is False


def test_secrets_are_redacted_before_storage() -> None:
    buf = transcript.TranscriptBuffer("run-1", max_bytes=10_000_000)
    buf.append(
        "tool_call",
        tool_name="Bash",
        call_id="c1",
        input_data={"command": f"curl -H 'Authorization: {BEARER}' {GH_TOKEN}", "api_key": "abc123"},
    )
    buf.append("assistant_text", text=f"my key is {GH_TOKEN}")
    records = buf.after(0, 10)
    blob = repr(records)
    assert GH_TOKEN not in blob
    assert BEARER not in blob
    assert "abc123" not in blob
    assert "[REDACTED]" in blob
    assert records[0]["input"]["api_key"] == "[REDACTED]"


def test_byte_cap_writes_one_truncated_marker_then_drops() -> None:
    buf = transcript.TranscriptBuffer("run-1", max_bytes=600)
    for i in range(20):
        buf.append("assistant_text", text=f"block {i} " + "z" * 80)
    records = buf.after(0, 100)
    kinds = [r["type"] for r in records]
    assert kinds.count("truncated") == 1
    assert kinds[-1] == "truncated"
    assert buf.truncated is True
    assert buf.dropped_records > 0
    assert buf.last_seq == len(records)


def test_runner_delivers_batches_and_last_seq_when_enabled() -> None:
    async def scenario() -> None:
        async def executor(payload: JobInitPayload) -> JobResult:
            transcript.emit("user", text="prompt")
            for i in range(3):
                transcript.emit("assistant_text", text=f"turn {i}")
            return JobResult(success=True, summary="ok")

        runner = JobRunner(executor, resolver=None, required_secrets=())
        accepted = await runner.submit(_payload("job-a", max_bytes=10_000_000))
        assert accepted.job_id == "job-a"
        assert runner._task is not None
        await runner._task
        first = runner.get("job-a", transcript_after=0, transcript_limit=2)
        assert first.transcript_last_seq == 4
        assert [r["seq"] for r in first.transcript] == [1, 2]
        rest = runner.get("job-a", transcript_after=2, transcript_limit=500)
        assert [r["seq"] for r in rest.transcript] == [3, 4]

    asyncio.run(scenario())


def test_runner_without_cursor_reports_last_seq_only() -> None:
    async def scenario() -> None:
        async def executor(payload: JobInitPayload) -> JobResult:
            transcript.emit("user", text="prompt")
            return JobResult(success=True, summary="ok")

        runner = JobRunner(executor, resolver=None, required_secrets=())
        await runner.submit(_payload("job-b", max_bytes=10_000_000))
        await runner._task
        status = runner.get("job-b")
        assert status.transcript_last_seq == 1
        assert status.transcript == []

    asyncio.run(scenario())


def test_runner_disabled_job_records_nothing() -> None:
    async def scenario() -> None:
        async def executor(payload: JobInitPayload) -> JobResult:
            transcript.emit("user", text="prompt")
            return JobResult(success=True, summary="ok")

        runner = JobRunner(executor, resolver=None, required_secrets=())
        await runner.submit(_payload("job-c", max_bytes=None))
        await runner._task
        status = runner.get("job-c", transcript_after=0)
        assert status.transcript_last_seq is None
        assert status.transcript == []

    asyncio.run(scenario())

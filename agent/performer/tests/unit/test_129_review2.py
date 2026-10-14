"""Spec 129, second review, performer side: batches are served off the loop, and
chunks without a message id are not counted once per chunk."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from performer import transcript
from performer.backends.claude_code import ClaudeCodeBackend
from performer.server import create_app
from performer.server.models import JobInitPayload, JobResult


def _payload(job_id: str) -> dict[str, Any]:
    return JobInitPayload(
        job_id=job_id,
        card_id="PVTI_review2",
        role="implementer",
        backend="claude_code",
        persona="coder",
        repo_url="https://github.com/acme/widgets",
        branch="main",
        transcript_max_bytes=1_000_000,
    ).model_dump(mode="json")


async def _executor(payload: JobInitPayload) -> JobResult:
    transcript.emit("user", text="prompt")
    transcript.emit("assistant_text", text="reply")
    return JobResult(success=True, summary="ok")


async def _wait_terminal(client: AsyncClient, job_id: str) -> None:
    for _ in range(500):
        body = (await client.get(f"/jobs/{job_id}")).json()
        if body["state"] in {"succeeded", "failed", "cancelled"}:
            return
        await asyncio.sleep(0.01)


async def test_transcript_batches_are_served_on_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 2 (performer): redaction runs while a batch is served, on a worker
    thread, so the status endpoint and the running backend share the loop."""
    served_on_thread: list[str] = []
    real_to_thread: Callable[..., Any] = asyncio.to_thread

    async def spy(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        served_on_thread.append(getattr(func, "__name__", "?"))
        return await real_to_thread(func, *args, **kwargs)

    app = create_app(expected_token=None, executor=_executor)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/jobs", json=_payload("job-thread"))
        await _wait_terminal(client, "job-thread")
        monkeypatch.setattr(asyncio, "to_thread", spy)
        plain = await client.get("/jobs/job-thread")
        assert plain.status_code == 200
        assert "get" not in served_on_thread, "a plain status poll does not hop threads"
        batch = await client.get(
            "/jobs/job-thread",
            params={"transcript_after": 0, "transcript_limit": 10},
        )
        assert batch.status_code == 200
        assert len(batch.json()["transcript"]) == 2
        assert "get" in served_on_thread


@pytest.fixture
def buffer() -> Iterator[transcript.TranscriptBuffer]:
    buf = transcript.TranscriptBuffer("run-review2", max_bytes=10_000_000)
    transcript.set_active_buffer(buf)
    yield buf
    transcript.set_active_buffer(None)


def _assistant_without_id(output_tokens: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-test",
            "content": [{"type": "text", "text": "chunk"}],
            "usage": {
                "input_tokens": 10,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def test_chunks_without_a_message_id_are_written_once_with_the_last_usage(
    buffer: transcript.TranscriptBuffer,
) -> None:
    """Finding 7: a message with no id used to be written per chunk, counting its tokens
    once per chunk. Only the last chunk is written, at the result event."""
    backend = ClaudeCodeBackend()
    for output_tokens in (5, 20, 40):
        backend._handle_event(_assistant_without_id(output_tokens))
    backend._handle_event({"type": "result", "subtype": "success", "result": "done", "usage": {}})
    usage_records = [r for r in buffer.after(0, 100) if r["type"] == "usage"]
    message_scoped = [r for r in usage_records if r.get("scope") == "message"]
    assert len(message_scoped) == 1
    assert message_scoped[0]["usage"]["output_tokens"] == 40

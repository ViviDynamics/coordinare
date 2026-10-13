"""Spec 129 review fixes, performer side: best-effort capture, redaction, bounded batches."""
from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from performer import transcript
from performer.transcript_redaction import redact_text, redact_value

JOB_SECRET = "jobsecret-7f3a91c2"  # matches no token pattern; only exact-value redaction removes it
GH_TOKEN = "ghp_" + "d" * 36


@pytest.fixture
def buffer() -> Iterator[transcript.TranscriptBuffer]:
    buf = transcript.TranscriptBuffer(
        "run-g", max_bytes=10_000_000, secrets=lambda: [JOB_SECRET],
    )
    transcript.set_active_buffer(buf)
    yield buf
    transcript.set_active_buffer(None)


def test_emit_never_raises_when_capture_breaks(
    buffer: transcript.TranscriptBuffer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH 3 (performer side): a capture failure must not reach a backend's reader loop."""

    def explode(self: transcript.TranscriptBuffer, *args: object, **kwargs: object) -> None:
        message = "buffer broke"
        raise RuntimeError(message)

    monkeypatch.setattr(transcript.TranscriptBuffer, "append", explode)
    transcript.emit("assistant_text", text="still fine")  # must not raise


def test_best_effort_wraps_a_backend_helper_and_returns_none() -> None:
    @transcript.best_effort
    def helper(value: int) -> int:
        message = f"bad input {value}"
        raise ValueError(message)

    assert helper(3) is None


def test_job_secrets_and_credential_keys_never_reach_the_stored_record(
    buffer: transcript.TranscriptBuffer,
) -> None:
    """HIGH 4 (performer side): exact values, credential keys, JSON strings, env lines."""
    buffer.append(
        "tool_result",
        output_data={
            "github_token": GH_TOKEN,
            "payload": json.dumps({"password": "pw-json-value-42"}),
            "env": f"ANTHROPIC_AUTH_TOKEN=env-value-abc123\nJOB={JOB_SECRET}",
            "input_tokens": 9,
        },
        call_id="c1",
    )
    blob = json.dumps(buffer.after(0, 10))
    for secret in (GH_TOKEN, "pw-json-value-42", "env-value-abc123", JOB_SECRET):
        assert secret not in blob
    stored = buffer.after(0, 10)[0]["output"]
    assert stored["input_tokens"] == 9, "usage counts are not secrets"
    assert stored["github_token"] == "[REDACTED]"


def test_secret_provider_is_read_per_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token refreshed mid-run is redacted from its first use after the refresh."""
    current: list[str] = []
    buf = transcript.TranscriptBuffer("run-r", max_bytes=10_000_000, secrets=lambda: current)
    buf.append("assistant_text", text="before refresh: fresh-token-value-99")
    current.append("fresh-token-value-99")
    buf.append("assistant_text", text="after refresh: fresh-token-value-99")
    texts = [r["text"] for r in buf.after(0, 10)]
    assert "fresh-token-value-99" in texts[0]
    assert "fresh-token-value-99" not in texts[1]


def test_batches_are_byte_bounded_and_always_progress() -> None:
    buf = transcript.TranscriptBuffer("run-b", max_bytes=10_000_000, max_batch_bytes=500)
    for i in range(10):
        buf.append("assistant_text", text="x" * 200)
    first = buf.after(0, 500)
    assert 1 <= len(first) < 10
    total = sum(len(json.dumps(r)) for r in first)
    assert total <= 500 + 300, "the bound applies to what is returned, within one record"
    # A single record larger than the bound is still delivered alone.
    tiny = transcript.TranscriptBuffer("run-t", max_bytes=10_000_000, max_batch_bytes=10)
    tiny.append("assistant_text", text="y" * 100)
    assert len(tiny.after(0, 10)) == 1


def test_scope_is_carried_on_usage_records() -> None:
    buf = transcript.TranscriptBuffer("run-s", max_bytes=10_000_000)
    usage = transcript.TranscriptUsage(input_tokens=1, output_tokens=2)
    buf.append("usage", usage=usage, scope="message")
    buf.append("usage", usage=usage, scope="run")
    assert [r["scope"] for r in buf.after(0, 10)] == ["message", "run"]


def test_shared_redaction_handles_the_review_shapes() -> None:
    assert redact_text("ANTHROPIC_AUTH_TOKEN=abc123xyz") == "ANTHROPIC_AUTH_TOKEN=[REDACTED]"
    assert redact_text("key sk-proj-" + "q" * 30) == "key [REDACTED]"
    assert "[REDACTED]" in redact_text("Authorization: Basic dXNlcjpwYXNz")
    value = redact_value({"api-key": "dash-secret-1", "n": 2})
    assert value == {"api-key": "[REDACTED]", "n": 2}

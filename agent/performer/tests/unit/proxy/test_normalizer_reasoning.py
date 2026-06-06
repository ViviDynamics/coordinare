"""078 US1 — strip_reasoning normalizer (T012, written FIRST / must FAIL).

Generalizes the 073 ``ClaudeCodeShim`` thinking-block strip. Reasoning/thinking
blocks that leak into the response (Anthropic ``thinking`` content blocks; OpenAI
``reasoning_content`` deltas on qwen reasoners) must be dropped on BOTH the
non-streaming JSON path and the streaming SSE path, leaving the real assistant
output intact (073 regression, SC-002, FR-078-8, FR-078-3).
"""

from __future__ import annotations

import json

import pytest

from performer.proxy.normalizers.reasoning import StripReasoningNormalizer
from tests.unit.proxy.fixtures import (
    REASONING_ANTHROPIC_JSON,
    REASONING_ANTHROPIC_SSE_CHUNKS,
    REASONING_OPENAI_JSON,
    REASONING_OPENAI_SSE_CHUNKS,
)


@pytest.fixture()
def norm():
    return StripReasoningNormalizer()


def test_registry_key(norm):
    assert norm.key == "strip_reasoning"


# --- non-streaming JSON path --------------------------------------------- #


def test_json_anthropic_thinking_block_dropped(norm):
    out = norm.normalize_json(REASONING_ANTHROPIC_JSON)
    blocks = out["content"]
    assert all(b.get("type") != "thinking" for b in blocks), (
        "thinking content block must be stripped"
    )
    # the real text block survives unchanged
    texts = [b["text"] for b in blocks if b.get("type") == "text"]
    assert texts == ["The file imports os and sys."]


def test_json_openai_reasoning_content_dropped(norm):
    out = norm.normalize_json(REASONING_OPENAI_JSON)
    message = out["choices"][0]["message"]
    assert "reasoning_content" not in message
    # real content is untouched
    assert message["content"] == "The file imports os and sys."


def test_json_clean_response_unchanged(norm):
    clean = {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"},
             "finish_reason": "stop"}
        ]
    }
    assert norm.normalize_json(clean) == clean


# --- streaming SSE path --------------------------------------------------- #


def _frames(out: str) -> list[dict]:
    payloads = []
    for frame in out.split("\n\n"):
        line = next(
            (ln for ln in frame.split("\n") if ln.startswith("data:")), None
        )
        if line is None:
            continue
        body = line[len("data:") :].strip()
        if not body or body == "[DONE]":
            continue
        payloads.append(json.loads(body))
    return payloads


def _run(flt, chunks) -> str:
    emitted = bytearray()
    for chunk in chunks:
        emitted.extend(flt.feed(chunk))
    emitted.extend(flt.flush())
    return bytes(emitted).decode("utf-8")


def test_sse_anthropic_thinking_block_stripped(norm):
    out = _run(norm.sse_filter(), REASONING_ANTHROPIC_SSE_CHUNKS)
    assert "thinking" not in out, "thinking deltas must not survive in the SSE stream"
    assert "reasoning" not in out
    # the real text delta is still streamed through
    assert "The file imports os." in out


def test_sse_openai_reasoning_content_stripped(norm):
    out = _run(norm.sse_filter(), REASONING_OPENAI_SSE_CHUNKS)
    assert "reasoning_content" not in out
    # real content deltas survive
    assert "The file imports os." in out
    # no payload carries a reasoning_content field
    for payload in _frames(out):
        delta = payload["choices"][0].get("delta", {})
        assert "reasoning_content" not in delta

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


# --- empty-content fallback (harmony/gpt-oss truncated mid-reasoning) ----- #
#
# When a reasoning model leaves `content` empty and puts everything in
# `reasoning_content` (truncated mid-thinking, or answer leaked into the
# reasoning channel), stripping reasoning unconditionally would hand the agent
# an EMPTY response -> "unknown" verdict -> qa terminal error. The normalizer
# must instead PROMOTE the reasoning into content so the harness gets parseable
# text. The healthy path (content present) is unchanged: reasoning is dropped.


def test_json_openai_empty_content_promotes_reasoning(norm):
    body = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": '{"verdict":"pass"}',
                },
                "finish_reason": "length",
            }
        ]
    }
    out = norm.normalize_json(body)
    message = out["choices"][0]["message"]
    assert "reasoning_content" not in message
    # the reasoning text is promoted into content (no longer empty)
    assert message["content"] == '{"verdict":"pass"}'


def test_json_openai_missing_content_promotes_reasoning(norm):
    body = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "reasoning_content": "the answer"},
                "finish_reason": "stop",
            }
        ]
    }
    out = norm.normalize_json(body)
    message = out["choices"][0]["message"]
    assert "reasoning_content" not in message
    assert message["content"] == "the answer"


def test_json_openai_nonempty_content_still_strips(norm):
    """Healthy path unchanged: content present -> reasoning dropped, not promoted."""
    body = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "the real answer",
                    "reasoning_content": "thinking noise",
                },
                "finish_reason": "stop",
            }
        ]
    }
    out = norm.normalize_json(body)
    message = out["choices"][0]["message"]
    assert "reasoning_content" not in message
    assert message["content"] == "the real answer"


_OPENAI_SSE_REASONING_ONLY: list[bytes] = [
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
    b'"delta":{"role":"assistant","reasoning_content":"part one "},"finish_reason":null}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
    b'"delta":{"reasoning_content":"part two"},"finish_reason":null}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
    b'"delta":{},"finish_reason":"length"}]}\n\n',
    b"data: [DONE]\n\n",
]


def test_sse_openai_empty_content_promotes_reasoning(norm):
    """Stream with only reasoning deltas and no content must yield a content
    delta carrying the buffered reasoning, so the harness isn't handed nothing."""
    out = _run(norm.sse_filter(), _OPENAI_SSE_REASONING_ONLY)
    assert "reasoning_content" not in out
    # the buffered reasoning surfaces as content
    contents = [
        p["choices"][0].get("delta", {}).get("content")
        for p in _frames(out)
        if p["choices"][0].get("delta", {}).get("content")
    ]
    assert "".join(contents) == "part one part two"
    # the finish frame is still present and well-formed
    assert any(
        p["choices"][0].get("finish_reason") == "length" for p in _frames(out)
    )


def test_sse_openai_content_present_no_promotion(norm):
    """Healthy stream (content delta present) is unchanged — reasoning dropped,
    content survives, no duplication from promotion."""
    out = _run(norm.sse_filter(), REASONING_OPENAI_SSE_CHUNKS)
    contents = [
        p["choices"][0].get("delta", {}).get("content")
        for p in _frames(out)
        if p["choices"][0].get("delta", {}).get("content")
    ]
    # exactly the one real content delta, not the reasoning text
    assert contents == ["The file imports os."]

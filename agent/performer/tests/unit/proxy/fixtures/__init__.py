"""077-derived regression fixtures for the self-hosted robustness layer (spec 078).

These capture the two real self-hosted-stack artifacts the layer normalizes:

1. **Leaked harmony tool calls** (openclaw / gpt-oss via LiteLLM, #13300/#17246):
   LiteLLM's streaming harmony->tool_calls transform leaks the raw
   ``<|channel|>commentary to=functions.<tool> ... <|message|>{args}<|call|>``
   text into the assistant ``content`` instead of emitting structured
   ``tool_calls``. Provided as a non-streaming JSON body and as SSE deltas that
   split the harmony markers across chunk boundaries.

2. **Reasoning/thinking-block leakage** (claude_code on a qwen reasoner, the 073
   case): thinking/reasoning blocks surface to the CLI parser. Provided as an
   Anthropic-style JSON body with a ``thinking`` content block and as SSE events,
   plus an OpenAI-style ``reasoning_content`` delta variant for qwen.

Fixtures are exposed as module constants so tests import them directly. SSE
streams are provided as ``list[bytes]`` chunks (deliberately split mid-marker /
mid-frame) to exercise the stateful cross-chunk buffering contract (FR-078-3).
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 1. Harmony tool-call leakage (OpenAI chat.completions wire format)
# --------------------------------------------------------------------------- #

# A non-streaming response where the harmony commentary channel leaked verbatim
# into ``content`` rather than being parsed into ``tool_calls``.
HARMONY_LEAKED_JSON: dict = {
    "id": "chatcmpl-077harmony",
    "object": "chat.completion",
    "model": "gpt-oss:120b",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": (
                    "<|channel|>commentary to=functions.read_file "
                    '<|constrain|>json<|message|>{"path": "README.md"}<|call|>'
                ),
            },
            "finish_reason": "stop",
        }
    ],
}

# A clean response (no harmony markers) — the normalizer must pass this through
# byte-for-byte (fail-open on an unrecognized/already-clean shape, FR-078-9).
HARMONY_CLEAN_JSON: dict = {
    "id": "chatcmpl-clean",
    "object": "chat.completion",
    "model": "gpt-oss:120b",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Here is the summary."},
            "finish_reason": "stop",
        }
    ],
}

# SSE stream for the same leaked tool call, with the harmony markers and the JSON
# arguments split ACROSS delta/chunk boundaries (so a stateless filter would emit
# a half-parsed call). Each list element is one network chunk; chunk boundaries
# intentionally fall mid-marker and mid-frame.
HARMONY_LEAKED_SSE_CHUNKS: list[bytes] = [
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"content":"<|channel|>comment',
    b'ary to=functions.read_file <|constrain|>json<|mess'
    b'age|>{\\"path\\"'
    b': \\"REA"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"content":"DME.md\\"}<|call',
    b'|>"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n",
]

# The tool the harmony fixtures encode, for test assertions.
HARMONY_EXPECTED_TOOL_NAME = "read_file"
HARMONY_EXPECTED_ARGS = '{"path": "README.md"}'

# --------------------------------------------------------------------------- #
# 2. Reasoning / thinking-block leakage
# --------------------------------------------------------------------------- #

# Anthropic-style JSON body with a leaked ``thinking`` content block (073 case).
REASONING_ANTHROPIC_JSON: dict = {
    "id": "msg_077reasoning",
    "type": "message",
    "role": "assistant",
    "model": "qwen3-coder",
    "content": [
        {"type": "thinking", "thinking": "Let me reason about the file first."},
        {"type": "text", "text": "The file imports os and sys."},
    ],
    "stop_reason": "end_turn",
}

# Anthropic SSE events: a thinking block (start/delta/stop at index 0) followed by
# a text block (index 1). The filter must drop the thinking block and renumber the
# text block to index 0. Split across chunk boundaries.
REASONING_ANTHROPIC_SSE_CHUNKS: list[bytes] = [
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_1","role":"assistant","content":[]}}\n\n'
    b"event: content_block_start\n"
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"think',
    b'ing","thinking":""}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"reasoning..."}}\n\n'
    b"event: content_block_stop\n"
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b"event: content_block_start\n",
    b'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"The file imports os."}}\n\n'
    b"event: content_block_stop\n"
    b'data: {"type":"content_block_stop","index":1}\n\n'
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n\n',
]

# OpenAI-style qwen variant: a non-streaming body carrying ``reasoning_content``
# alongside the real ``content``; the normalizer drops ``reasoning_content``.
REASONING_OPENAI_JSON: dict = {
    "id": "chatcmpl-qwenreason",
    "object": "chat.completion",
    "model": "qwen3-coder",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "reasoning_content": "First I will inspect the imports.",
                "content": "The file imports os and sys.",
            },
            "finish_reason": "stop",
        }
    ],
}

# OpenAI-style qwen SSE: reasoning_content deltas then content deltas, split.
REASONING_OPENAI_SSE_CHUNKS: list[bytes] = [
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
    b'"delta":{"role":"assistant","reasoning_content":"First I will"},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
    b'"delta":{"reasoning_content":" inspect imports."},"finish_re',
    b'ason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
    b'"delta":{"content":"The file imports os."},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,'
    b'"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n",
]

__all__ = [
    "HARMONY_LEAKED_JSON",
    "HARMONY_CLEAN_JSON",
    "HARMONY_LEAKED_SSE_CHUNKS",
    "HARMONY_EXPECTED_TOOL_NAME",
    "HARMONY_EXPECTED_ARGS",
    "REASONING_ANTHROPIC_JSON",
    "REASONING_ANTHROPIC_SSE_CHUNKS",
    "REASONING_OPENAI_JSON",
    "REASONING_OPENAI_SSE_CHUNKS",
]

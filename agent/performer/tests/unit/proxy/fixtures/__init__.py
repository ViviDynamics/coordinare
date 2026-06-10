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

# --------------------------------------------------------------------------- #
# 3. Stub OpenAI-wire upstream (spec 084 translate strategy)
# --------------------------------------------------------------------------- #
#
# Deterministic canned replies an OpenAI-wire upstream (Ollama-direct gpt-oss)
# would return to a translated ``/v1/chat/completions`` request. Used by the
# translate-strategy tests so the full request-translate -> forward ->
# response-translate round trip runs with NO live network (Constitution II).
# The harmony-leak case the translate path must survive already exists above as
# ``HARMONY_LEAKED_JSON`` / ``HARMONY_LEAKED_SSE_CHUNKS`` (the LiteLLM #17246
# failure shape) — these add the clean text-completion shapes plus a usage block
# so response translation can assert the ``prompt_tokens``->``input_tokens`` /
# ``completion_tokens``->``output_tokens`` rename and ``finish_reason`` mapping.

# Canned non-streaming chat.completion: a plain assistant text turn with a usage
# block. ``finish_reason: "stop"`` must translate to Anthropic ``end_turn``.
STUB_OPENAI_NONSTREAM_JSON: dict = {
    "id": "chatcmpl-stub-nonstream",
    "object": "chat.completion",
    "model": "gpt-oss:120b",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "The README documents the build steps.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 27,
        "completion_tokens": 9,
        "total_tokens": 36,
    },
}

# Canned streaming chat.completion.chunk SSE for the same plain text turn: a role
# delta, two content deltas (the second split across a chunk boundary to exercise
# the stateful cross-chunk buffer), a terminal ``finish_reason: "stop"`` chunk,
# and the ``[DONE]`` sentinel. The translate SSE filter must emit the Anthropic
# ``message_start`` -> ``content_block_start``/``_delta``/``_stop`` ->
# ``message_delta`` -> ``message_stop`` sequence from this.
STUB_OPENAI_STREAM_SSE_CHUNKS: list[bytes] = [
    b'data: {"id":"chatcmpl-stub","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stub","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"content":"The README "},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stub","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"content":"documents the bu',
    b'ild steps."},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stub","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n",
]

# Canned non-streaming chat.completion with a STRUCTURED tool call (the shape an
# OpenAI-wire upstream emits when tools work correctly — contrast with the
# harmony-leak fixtures). ``finish_reason: "tool_calls"`` must translate to
# Anthropic ``tool_use``; ``function.arguments`` is a JSON string the response
# translator parses into the ``tool_use`` block ``input``.
STUB_OPENAI_TOOLCALL_JSON: dict = {
    "id": "chatcmpl-stub-toolcall",
    "object": "chat.completion",
    "model": "gpt-oss:120b",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stub_0",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "README.md"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {
        "prompt_tokens": 31,
        "completion_tokens": 12,
        "total_tokens": 43,
    },
}

# Canned non-2xx upstream reply (status + body) for FR-012: the shim surfaces it
# verbatim to the CLI and the response translator is NOT invoked.
STUB_OPENAI_ERROR_STATUS = 503
STUB_OPENAI_ERROR_BODY: bytes = (
    b'{"error":{"message":"model gpt-oss:120b is loading","type":"server_error"}}'
)

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
    "STUB_OPENAI_NONSTREAM_JSON",
    "STUB_OPENAI_STREAM_SSE_CHUNKS",
    "STUB_OPENAI_TOOLCALL_JSON",
    "STUB_OPENAI_ERROR_STATUS",
    "STUB_OPENAI_ERROR_BODY",
]

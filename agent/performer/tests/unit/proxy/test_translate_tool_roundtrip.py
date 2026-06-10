"""084 US2 — tools / tool_choice / tool_use / tool_result round trip (T015).

TDD: written BEFORE the US2 tool mappings land in ``request.py`` and the
``tool_use`` streaming emission lands in ``sse.py``; the request-side and
SSE-side cases MUST FAIL first (the non-streaming ``tool_use`` JSON emission
already landed in US1's ``response.py`` and stays green — that is intentional,
those assertions guard against regression).

Covers FR-004 from ``contracts/request-translation.md`` and
``contracts/response-translation.md``:

* request ``tools`` map: ``input_schema`` → ``function.parameters``, ``name`` →
  ``function.name``, ``description`` carried, constant ``type: "function"``
* ``tool_choice`` shape map: ``auto`` → ``"auto"``, ``any`` → ``"required"``,
  ``{type:tool,name:N}`` → ``{type:function,function:{name:N}}``, absent → omit
* assistant ``tool_use`` block → OpenAI ``tool_calls[]`` (``input`` →
  JSON-stringified ``function.arguments``)
* user ``tool_result`` block → OpenAI ``{role:"tool", tool_call_id, content}``
* response/SSE ``tool_use`` block emission, composing with ``harmony_tool_calls``
* degenerate non-JSON ``function.arguments`` → ``input: {}`` (never a drop)
* zero harmony markers survive into the CLI-facing output
"""

from __future__ import annotations

import json

from performer.proxy.normalizers.harmony import HarmonyToolCallsNormalizer
from performer.proxy.translate.request import translate_request
from performer.proxy.translate.response import translate_response
from performer.proxy.translate.sse import TranslatingSSEFilter

from .fixtures import (
    HARMONY_EXPECTED_TOOL_NAME,
    HARMONY_LEAKED_JSON,
    STUB_OPENAI_TOOLCALL_JSON,
)


# --- request: tools[] mapping ----------------------------------------------- #


def test_request_tools_map_input_schema_to_function_parameters():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    out = translate_request(
        {
            "model": "claude-3-5-sonnet",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "read it"}],
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read a file.",
                    "input_schema": schema,
                }
            ],
        }
    )
    assert out["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": schema,
            },
        }
    ]


def test_request_no_tools_key_when_absent():
    out = translate_request(
        {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]}
    )
    assert "tools" not in out


# --- request: tool_choice shape map ----------------------------------------- #


def _req_with_tool_choice(tool_choice) -> dict:
    return translate_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "t", "input_schema": {"type": "object"}}],
            "tool_choice": tool_choice,
        }
    )


def test_tool_choice_auto_maps_to_string_auto():
    assert _req_with_tool_choice({"type": "auto"})["tool_choice"] == "auto"


def test_tool_choice_any_maps_to_required():
    assert _req_with_tool_choice({"type": "any"})["tool_choice"] == "required"


def test_tool_choice_named_tool_maps_to_openai_named_function():
    out = _req_with_tool_choice({"type": "tool", "name": "read_file"})
    assert out["tool_choice"] == {
        "type": "function",
        "function": {"name": "read_file"},
    }


def test_tool_choice_absent_is_omitted():
    out = translate_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "t", "input_schema": {"type": "object"}}],
        }
    )
    assert "tool_choice" not in out


# --- request: assistant tool_use block → tool_calls[] ----------------------- #


def test_request_tool_use_block_becomes_tool_calls_entry():
    out = translate_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {"role": "user", "content": "read it"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Sure."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "read_file",
                            "input": {"path": "README.md"},
                        },
                    ],
                },
            ],
        }
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"][0]
    assert assistant["tool_calls"] == [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": "README.md"}),
            },
        }
    ]
    # text alongside the tool_use is preserved on the assistant message content
    assert assistant["content"] == "Sure."


def test_request_assistant_tool_use_only_has_null_content():
    out = translate_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_2",
                            "name": "ping",
                            "input": {},
                        }
                    ],
                }
            ],
        }
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"][0]
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["function"]["name"] == "ping"


# --- request: user tool_result block → tool message ------------------------- #


def test_request_tool_result_block_becomes_tool_message():
    out = translate_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "file contents here",
                        }
                    ],
                }
            ],
        }
    )
    tool_msgs = [m for m in out["messages"] if m["role"] == "tool"]
    assert tool_msgs == [
        {
            "role": "tool",
            "tool_call_id": "toolu_1",
            "content": "file contents here",
        }
    ]


def test_request_tool_result_block_array_content_joined():
    out = translate_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_9",
                            "content": [
                                {"type": "text", "text": "line one"},
                                {"type": "text", "text": "line two"},
                            ],
                        }
                    ],
                }
            ],
        }
    )
    tool_msg = [m for m in out["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"] == "line one\nline two"


# --- response (non-stream) tool_use, composing with harmony ----------------- #


def test_response_tool_calls_become_tool_use_block():
    body = translate_response(dict(STUB_OPENAI_TOOLCALL_JSON))
    tool_uses = [b for b in body["content"] if b["type"] == "tool_use"]
    assert tool_uses == [
        {
            "type": "tool_use",
            "id": "call_stub_0",
            "name": "read_file",
            "input": {"path": "README.md"},
        }
    ]
    assert body["stop_reason"] == "tool_use"


def test_response_degenerate_arguments_yield_empty_input_not_drop():
    degenerate = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {"name": "f", "arguments": "not json {"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    body = translate_response(degenerate)
    tool_uses = [b for b in body["content"] if b["type"] == "tool_use"]
    assert tool_uses and tool_uses[0]["input"] == {}
    assert tool_uses[0]["name"] == "f"  # the block is kept, not silently dropped


def test_response_composes_harmony_then_translate_no_marker_leak():
    normalized = HarmonyToolCallsNormalizer().normalize_json(dict(HARMONY_LEAKED_JSON))
    body = translate_response(normalized)
    tool_uses = [b for b in body["content"] if b["type"] == "tool_use"]
    assert tool_uses and tool_uses[0]["name"] == HARMONY_EXPECTED_TOOL_NAME
    assert "<|channel|>" not in json.dumps(body)


# --- streaming tool_use emission (input_json_delta) ------------------------- #


_TOOLCALL_SSE_CHUNKS = [
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_s0",'
    b'"type":"function","function":{"name":"read_file","arguments":""}}]},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"tool_calls":[{"index":0,"function":'
    b'{"arguments":"{\\"path\\":"}}]},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{"tool_calls":[{"index":0,"function":'
    b'{"arguments":"\\"README.md\\"}"}}]},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":'
    b'[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    b"data: [DONE]\n\n",
]


def _drive(chunks: list[bytes]) -> bytes:
    f = TranslatingSSEFilter()
    out = bytearray()
    for chunk in chunks:
        out += f.feed(chunk)
    out += f.flush()
    return bytes(out)


def _frames(raw: bytes) -> list[str]:
    return [fr for fr in raw.decode("utf-8").split("\n\n") if fr.strip()]


def _event_kinds(raw: bytes) -> list[str]:
    kinds = []
    for frame in _frames(raw):
        for line in frame.splitlines():
            if line.startswith("event: "):
                kinds.append(line[len("event: "):])
    return kinds


def _payloads(raw: bytes) -> list[dict]:
    out = []
    for frame in _frames(raw):
        for line in frame.splitlines():
            if line.startswith("data: ") and line.strip() != "data: [DONE]":
                out.append(json.loads(line[len("data: "):]))
    return out


def test_stream_tool_use_block_start_carries_id_and_name():
    payloads = _payloads(_drive(_TOOLCALL_SSE_CHUNKS))
    starts = [
        p
        for p in payloads
        if p.get("type") == "content_block_start"
        and p["content_block"]["type"] == "tool_use"
    ]
    assert starts
    assert starts[0]["content_block"]["id"] == "call_s0"
    assert starts[0]["content_block"]["name"] == "read_file"


def test_stream_tool_use_arguments_emitted_as_input_json_delta():
    payloads = _payloads(_drive(_TOOLCALL_SSE_CHUNKS))
    partial = "".join(
        p["delta"]["partial_json"]
        for p in payloads
        if p.get("type") == "content_block_delta"
        and p["delta"].get("type") == "input_json_delta"
    )
    assert json.loads(partial) == {"path": "README.md"}


def test_stream_tool_calls_finish_reason_maps_to_tool_use():
    payloads = _payloads(_drive(_TOOLCALL_SSE_CHUNKS))
    deltas = [p for p in payloads if p.get("type") == "message_delta"]
    assert deltas and deltas[-1]["delta"]["stop_reason"] == "tool_use"


def test_stream_tool_use_sequence_well_formed():
    kinds = _event_kinds(_drive(_TOOLCALL_SSE_CHUNKS))
    assert kinds[0] == "message_start"
    assert kinds[-1] == "message_stop"
    assert "content_block_start" in kinds
    assert kinds.index("content_block_stop") < kinds.index("message_delta")

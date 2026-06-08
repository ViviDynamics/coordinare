"""082 US9 / FR-011 — OpenAI Responses-API front-door wire format.

codex 0.137.0 is hard-locked to the OpenAI **Responses API** at request time: it
POSTs to ``/responses`` with a Responses-shaped body and parses a Responses-shaped
reply (JSON or SSE), regardless of the provider ``wire_api`` config. The
``DualModelProxy`` previously served only ``openai`` (chat-completions) and
``anthropic`` (messages) front doors, so proxied codex 404'd at ``/responses``.

These tests pin the third front-door wire format ("responses"):

* ``to_llm_request_responses`` parses a Responses request body (``instructions`` →
  system, ``input`` as string OR an array of message / function_call /
  function_call_output items, flattened ``tools``) into the canonical ``LLMRequest``.
* ``assemble_json`` / ``assemble_sse`` with ``wire_format="responses"`` render an
  ``LLMResponse`` back into a Responses ``object: "response"`` body (JSON) and the
  documented Responses SSE event stream.

The parse/assemble functions are pure — no network. Route-level acceptance (the
proxy serving ``/responses`` + ``/v1/responses``) lives in
``test_dual_model_proxy_routes.py``.
"""
from __future__ import annotations

import json

from performer.proxy.assembler import assemble_json, assemble_sse
from performer.proxy.llm_turn import LLMResponse, ToolCall
from performer.proxy.upstreams import to_llm_request_responses

# --- request parsing -------------------------------------------------------


def test_parse_input_string_becomes_user_message() -> None:
    req = to_llm_request_responses({"input": "hello world"})
    assert len(req.messages) == 1
    assert req.messages[0].role == "user"
    assert req.messages[0].content == "hello world"


def test_parse_instructions_becomes_system_message() -> None:
    req = to_llm_request_responses(
        {"instructions": "You are a reviewer.", "input": "review this"}
    )
    assert req.messages[0].role == "system"
    assert req.messages[0].content == "You are a reviewer."
    assert req.messages[1].role == "user"
    assert req.messages[1].content == "review this"


def test_parse_input_array_of_message_items() -> None:
    req = to_llm_request_responses(
        {
            "input": [
                {"type": "message", "role": "user", "content": "first"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "second"}],
                },
            ]
        }
    )
    assert [m.role for m in req.messages] == ["user", "assistant"]
    assert req.messages[0].content == "first"
    assert req.messages[1].content == "second"


def test_parse_input_text_content_blocks() -> None:
    """A user message may carry ``input_text`` content blocks rather than a string."""
    req = to_llm_request_responses(
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "part-a "},
                        {"type": "input_text", "text": "part-b"},
                    ],
                }
            ]
        }
    )
    assert req.messages[0].content == "part-a part-b"


def test_parse_function_call_and_output_items() -> None:
    req = to_llm_request_responses(
        {
            "input": [
                {"type": "message", "role": "user", "content": "run it"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "shell",
                    "arguments": '{"cmd": "ls"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "file.txt",
                },
            ]
        }
    )
    # user, assistant(tool_call), tool(result)
    assistant = req.messages[1]
    assert assistant.role == "assistant"
    assert len(assistant.tool_calls) == 1
    assert assistant.tool_calls[0].id == "call_1"
    assert assistant.tool_calls[0].name == "shell"
    assert assistant.tool_calls[0].arguments == {"cmd": "ls"}
    tool_msg = req.messages[2]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "call_1"
    assert tool_msg.content == "file.txt"


def test_parse_flattened_function_tools() -> None:
    """Responses tools are flat (``{type, name, description, parameters}``) — not
    nested under a ``function`` key like chat-completions."""
    req = to_llm_request_responses(
        {
            "input": "go",
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "description": "run a shell command",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
    )
    assert len(req.tools) == 1
    assert req.tools[0].name == "shell"
    assert req.tools[0].description == "run a shell command"
    assert req.tools[0].parameters == {"type": "object", "properties": {}}


def test_parse_stream_flag() -> None:
    assert to_llm_request_responses({"input": "x", "stream": True}).stream is True
    assert to_llm_request_responses({"input": "x"}).stream is False


# --- JSON assembly ---------------------------------------------------------


def test_assemble_json_message_output() -> None:
    resp = LLMResponse(content="the answer")
    out = assemble_json(resp, expose_plan_as="drop", wire_format="responses")
    assert out["object"] == "response"
    assert out["status"] == "completed"
    msgs = [item for item in out["output"] if item["type"] == "message"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"][0]["type"] == "output_text"
    assert msgs[0]["content"][0]["text"] == "the answer"


def test_assemble_json_plan_as_reasoning() -> None:
    resp = LLMResponse(content="answer", reasoning="my plan")
    out = assemble_json(resp, expose_plan_as="thinking", wire_format="responses")
    reasoning = [item for item in out["output"] if item["type"] == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["summary"][0]["text"] == "my plan"


def test_assemble_json_plan_prepend_content() -> None:
    resp = LLMResponse(content="answer", reasoning="my plan")
    out = assemble_json(resp, expose_plan_as="prepend_content", wire_format="responses")
    msg = next(item for item in out["output"] if item["type"] == "message")
    assert msg["content"][0]["text"] == "my plan\n\nanswer"
    assert not [item for item in out["output"] if item["type"] == "reasoning"]


def test_assemble_json_tool_calls_become_function_call_items() -> None:
    resp = LLMResponse(
        content="",
        tool_calls=(ToolCall(id="call_9", name="shell", arguments={"cmd": "ls"}),),
    )
    out = assemble_json(resp, expose_plan_as="drop", wire_format="responses")
    fcs = [item for item in out["output"] if item["type"] == "function_call"]
    assert len(fcs) == 1
    assert fcs[0]["call_id"] == "call_9"
    assert fcs[0]["name"] == "shell"
    assert json.loads(fcs[0]["arguments"]) == {"cmd": "ls"}


# --- SSE assembly ----------------------------------------------------------


def _event_types(events: list[str]) -> list[str]:
    types = []
    for block in events:
        for line in block.splitlines():
            if line.startswith("data: "):
                payload = line[len("data: ") :]
                if payload == "[DONE]":
                    continue
                types.append(json.loads(payload).get("type"))
    return types


def test_assemble_sse_brackets_with_created_and_completed() -> None:
    resp = LLMResponse(content="hi")
    events = assemble_sse(resp, expose_plan_as="drop", wire_format="responses")
    types = _event_types(events)
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert "response.output_text.delta" in types


def test_assemble_sse_streams_text_delta() -> None:
    resp = LLMResponse(content="streamed text")
    events = assemble_sse(resp, expose_plan_as="drop", wire_format="responses")
    deltas = []
    for block in events:
        for line in block.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                payload = json.loads(line[6:])
                if payload.get("type") == "response.output_text.delta":
                    deltas.append(payload["delta"])
    assert "".join(deltas) == "streamed text"


def test_assemble_sse_emits_function_call_arguments() -> None:
    resp = LLMResponse(
        content="",
        tool_calls=(ToolCall(id="call_3", name="shell", arguments={"cmd": "ls"}),),
    )
    events = assemble_sse(resp, expose_plan_as="drop", wire_format="responses")
    types = _event_types(events)
    assert "response.function_call_arguments.delta" in types
    assert "response.output_item.added" in types
    assert "response.completed" in types


def test_assemble_sse_completed_carries_final_output() -> None:
    """The terminal ``response.completed`` must carry the fully-populated output so a
    client that ignores deltas can still read the final message."""
    resp = LLMResponse(content="final")
    events = assemble_sse(resp, expose_plan_as="drop", wire_format="responses")
    completed = None
    for block in events:
        for line in block.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                payload = json.loads(line[6:])
                if payload.get("type") == "response.completed":
                    completed = payload
    assert completed is not None
    msg = next(i for i in completed["response"]["output"] if i["type"] == "message")
    assert msg["content"][0]["text"] == "final"

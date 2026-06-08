"""080 — ResponseAssembler JSON rendering across formats × expose_plan_as (T017/T021)."""

from __future__ import annotations

import json

import pytest

from performer.proxy.assembler import assemble_json
from performer.proxy.llm_turn import LLMResponse, ToolCall

_RESP = LLMResponse(content="the answer", tool_calls=(ToolCall("c1", "read", {"p": "x"}),), reasoning="THE PLAN")


@pytest.mark.parametrize("wire", ["openai", "anthropic"])
def test_tool_calls_preserved(wire):
    body = assemble_json(_RESP, expose_plan_as="drop", wire_format=wire)
    if wire == "openai":
        msg = body["choices"][0]["message"]
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"p": "x"}
        assert body["choices"][0]["finish_reason"] == "tool_calls"
    else:
        tu = [b for b in body["content"] if b["type"] == "tool_use"][0]
        assert tu["input"] == {"p": "x"}
        assert body["stop_reason"] == "tool_use"


def test_openai_thinking_goes_to_reasoning_content():
    body = assemble_json(_RESP, expose_plan_as="thinking", wire_format="openai")
    msg = body["choices"][0]["message"]
    assert msg["reasoning_content"] == "THE PLAN"
    assert msg["content"] == "the answer"  # plan NOT in content


def test_anthropic_thinking_adds_thinking_block_first():
    body = assemble_json(_RESP, expose_plan_as="thinking", wire_format="anthropic")
    assert body["content"][0] == {"type": "thinking", "thinking": "THE PLAN"}
    assert any(b["type"] == "text" and b["text"] == "the answer" for b in body["content"])


@pytest.mark.parametrize("wire", ["openai", "anthropic"])
def test_prepend_content_merges_plan_into_text(wire):
    body = assemble_json(_RESP, expose_plan_as="prepend_content", wire_format=wire)
    text = (
        body["choices"][0]["message"]["content"] if wire == "openai"
        else [b for b in body["content"] if b["type"] == "text"][0]["text"]
    )
    assert text.startswith("THE PLAN")
    assert "the answer" in text
    # no separate thinking channel when prepending
    if wire == "anthropic":
        assert all(b["type"] != "thinking" for b in body["content"])


@pytest.mark.parametrize("wire", ["openai", "anthropic"])
def test_drop_omits_plan_entirely(wire):
    body = assemble_json(_RESP, expose_plan_as="drop", wire_format=wire)
    blob = json.dumps(body)
    assert "THE PLAN" not in blob


def test_no_plan_no_tool_calls_is_plain_message():
    body = assemble_json(LLMResponse(content="hi"), expose_plan_as="thinking", wire_format="openai")
    msg = body["choices"][0]["message"]
    assert msg["content"] == "hi"
    assert "reasoning_content" not in msg
    assert body["choices"][0]["finish_reason"] == "stop"


# --- 082 FR-013: openai ChatCompletion envelope completeness --------------- #
#
# A strict OpenAI client (junie's OpenAICompletion deserializer) rejects a body
# missing the top-level envelope fields (id/object/created/model) — the dual
# proxy's `082r10d` "Failed to build 'issue.md.junie_standalone'" after two
# successful 200 calls. Lenient clients (opencode/openclaw) tolerate the bare
# {"choices": [...]} body; junie does not. The envelope must be complete.


def test_openai_body_carries_full_chat_completion_envelope():
    """The openai wire body MUST be a complete ChatCompletion object, not a bare
    {"choices": [...]}. Strict clients reject missing id/object/created/model."""
    body = assemble_json(LLMResponse(content="hi"), expose_plan_as="drop", wire_format="openai")
    assert body["object"] == "chat.completion"
    assert isinstance(body["id"], str) and body["id"]
    assert isinstance(body["created"], int) and body["created"] >= 0
    assert "model" in body


def test_openai_envelope_echoes_upstream_id_model_created_when_present():
    """When the upstream exec response is preserved in ``raw``, its real
    id/model/created are echoed back so the client sees a faithful envelope."""
    raw = {
        "id": "chatcmpl-upstream-xyz",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "spark/qwen3.6:35b",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
    resp = LLMResponse(content="hi", raw=raw)
    body = assemble_json(resp, expose_plan_as="drop", wire_format="openai")
    assert body["id"] == "chatcmpl-upstream-xyz"
    assert body["model"] == "spark/qwen3.6:35b"
    assert body["created"] == 1234567890
    assert body["object"] == "chat.completion"
    # the merged choices still reflect the assembled message, not the raw passthrough
    assert body["choices"][0]["message"]["content"] == "hi"
    assert body["choices"][0]["finish_reason"] == "stop"

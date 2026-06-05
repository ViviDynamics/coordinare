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

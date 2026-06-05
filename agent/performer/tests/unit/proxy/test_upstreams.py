"""080 — HttpUpstream wire adapters (render/parse) + httpx call (T016/T020)."""

from __future__ import annotations

import json

import httpx
import pytest

from performer.proxy.llm_turn import LLMRequest, Message, ToolCall, ToolSchema
from performer.proxy.upstreams import (
    HttpUpstream,
    UpstreamError,
    parse_anthropic,
    parse_openai,
    render_anthropic,
    render_openai,
)


def _req():
    return LLMRequest(
        messages=(Message.system("be terse"), Message.user("fix it")),
        tools=(ToolSchema(name="read", description="read file", parameters={"type": "object"}),),
    )


# --- render ----------------------------------------------------------------


def test_render_openai_includes_tools_and_system_message():
    body = render_openai(_req(), "gpt", tools_enabled=True)
    assert body["model"] == "gpt"
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert body["tools"][0]["function"]["name"] == "read"


def test_render_openai_hides_tools_when_disabled():
    assert "tools" not in render_openai(_req(), "gpt", tools_enabled=False)


def test_render_anthropic_lifts_system_and_maps_tools():
    body = render_anthropic(_req(), "claude", tools_enabled=True)
    assert body["system"] == "be terse"  # system is top-level, not a message
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["tools"][0]["name"] == "read"
    assert "input_schema" in body["tools"][0]
    assert body["max_tokens"] > 0  # anthropic requires it


def test_inbound_anthropic_tool_result_parses_is_error_flag():
    from performer.proxy.upstreams import to_llm_request_anthropic

    body = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "c1", "content": "ok", "is_error": True},
    ]}]}
    req = to_llm_request_anthropic(body)
    tool_msg = [m for m in req.messages if m.role == "tool"][0]
    assert tool_msg.is_error_tool_result is True
    # absent is_error → False
    body2 = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "c1", "content": "ok"},
    ]}]}
    assert to_llm_request_anthropic(body2).messages[0].is_error_tool_result is False


def test_render_anthropic_tool_result_becomes_user_block():
    req = LLMRequest(messages=(Message(role="tool", content="boom", tool_call_id="c1"),))
    body = render_anthropic(req, "claude", tools_enabled=False)
    blk = body["messages"][0]["content"][0]
    assert blk["type"] == "tool_result" and blk["tool_use_id"] == "c1"


# --- parse -----------------------------------------------------------------


def test_parse_openai_tool_calls():
    body = {
        "choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "read", "arguments": '{"p": "x"}'}}
        ]}}]
    }
    resp = parse_openai(body)
    assert resp.tool_calls[0] == ToolCall("c1", "read", {"p": "x"})


def test_parse_anthropic_text_thinking_tooluse():
    body = {"content": [
        {"type": "thinking", "thinking": "let me think"},
        {"type": "text", "text": "answer"},
        {"type": "tool_use", "id": "c1", "name": "read", "input": {"p": "x"}},
    ]}
    resp = parse_anthropic(body)
    assert resp.reasoning == "let me think"
    assert resp.content == "answer"
    assert resp.tool_calls[0].name == "read"


# --- httpx call (MockTransport) --------------------------------------------


@pytest.mark.asyncio
async def test_http_upstream_openai_roundtrip_and_auth():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("authorization")
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    up = HttpUpstream(
        name="t", model="gpt", wire_format="openai",
        base_url="http://spark:4000/v1", auth_token="sek", auth_style="bearer", client=client,
    )
    resp = await up.complete(_req(), tools_enabled=True)
    assert resp.content == "hi"
    assert captured["url"] == "http://spark:4000/v1/chat/completions"
    assert captured["auth"] == "Bearer sek"
    assert captured["body"]["tools"]  # tools forwarded
    await client.aclose()


@pytest.mark.asyncio
async def test_http_upstream_anthropic_uses_x_api_key_header():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["x_api_key"] = req.headers.get("x-api-key")
        captured["version"] = req.headers.get("anthropic-version")
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    up = HttpUpstream(
        name="c", model="claude", wire_format="anthropic",
        auth_token="key", auth_style="x-api-key", client=client,
    )
    resp = await up.complete(_req(), tools_enabled=False)
    assert resp.content == "ok"
    assert captured["x_api_key"] == "key"
    assert captured["version"]
    assert captured["url"].endswith("/messages")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_upstream_raises_upstream_error_on_http_failure():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    up = HttpUpstream(name="t", model="m", base_url="http://x/v1", client=client)
    with pytest.raises(UpstreamError):
        await up.complete(_req())
    await client.aclose()

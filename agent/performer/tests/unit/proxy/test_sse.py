"""080 — SSE rendering (assemble_sse) + streaming through the proxy shell (FR-014)."""

from __future__ import annotations

import json

import httpx
import pytest

from performer.proxy.assembler import assemble_sse
from performer.proxy.dual_model_proxy import DualModelProxy
from performer.proxy.llm_turn import LLMResponse, ToolCall

_RESP = LLMResponse(content="answer", tool_calls=(ToolCall("c1", "edit", {"p": "x"}),), reasoning="PLAN")


def _data_payloads(events: list[str]) -> list[dict]:
    out = []
    for ev in events:
        for line in ev.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[len("data: "):]))
    return out


# --- openai SSE ------------------------------------------------------------


def test_openai_sse_sequence_has_role_plan_content_toolcalls_finish_done():
    events = assemble_sse(_RESP, expose_plan_as="thinking", wire_format="openai")
    assert events[-1] == "data: [DONE]\n\n"
    payloads = _data_payloads(events)
    # first delta carries the role
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    # plan surfaced as reasoning_content (thinking)
    assert any(p["choices"][0]["delta"].get("reasoning_content") == "PLAN" for p in payloads)
    # content + tool_calls present; final chunk has finish_reason tool_calls
    assert any(p["choices"][0]["delta"].get("content") == "answer" for p in payloads)
    assert any("tool_calls" in p["choices"][0]["delta"] for p in payloads)
    assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_openai_sse_drop_omits_plan():
    events = assemble_sse(_RESP, expose_plan_as="drop", wire_format="openai")
    assert "PLAN" not in "".join(events)


# --- anthropic SSE ---------------------------------------------------------


def test_anthropic_sse_event_order_and_blocks():
    events = assemble_sse(_RESP, expose_plan_as="thinking", wire_format="anthropic")
    kinds = [ln[len("event: "):] for ev in events for ln in ev.splitlines() if ln.startswith("event: ")]
    assert kinds[0] == "message_start"
    assert kinds[-1] == "message_stop"
    assert "content_block_start" in kinds and "message_delta" in kinds
    payloads = _data_payloads(events)
    # a thinking block delta carries the plan
    assert any(p.get("delta", {}).get("type") == "thinking_delta"
               and p["delta"].get("thinking") == "PLAN" for p in payloads)
    # stop_reason reflects the tool call
    assert any(p.get("type") == "message_delta" and p["delta"].get("stop_reason") == "tool_use"
               for p in payloads)


# --- end-to-end streaming over the aiohttp shell ---------------------------


@pytest.mark.asyncio
async def test_proxy_streams_sse_when_requested():
    def handler(req: httpx.Request) -> httpx.Response:
        if '"tools"' not in req.content.decode():
            return httpx.Response(200, json={"choices": [{"message": {"content": "THE PLAN"}}]})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "done", "tool_calls": [{"id": "c1", "function": {"name": "edit", "arguments": "{}"}}]}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    orch = {
        "strategy": "always",
        "tool": {"name": "t", "model": "qwen", "wire_format": "openai", "base_url": "http://spark/v1"},
        "thinking": {"name": "th", "model": "gptoss", "wire_format": "openai", "base_url": "http://spark/v1"},
        "expose_plan_as": "thinking",
    }
    proxy = DualModelProxy(orchestration=orch, expose_plan_as="thinking", client=client)
    base = await proxy.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base}/v1/chat/completions",
                json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "go"}],
                      "tools": [{"type": "function", "function": {"name": "edit", "parameters": {}}}]},
            )
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            text = r.text
            assert text.rstrip().endswith("data: [DONE]")
            assert "reasoning_content" in text and "THE PLAN" in text
            assert "edit" in text
    finally:
        await proxy.stop()
        await client.aclose()

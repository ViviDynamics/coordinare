from __future__ import annotations

import json

import httpx
import pytest

from performer.proxy.dual_model_proxy import build_strategy
from performer.proxy.upstreams import (
    to_llm_request_anthropic,
    to_llm_request_openai,
    to_llm_request_responses,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("inbound_wire", ["openai", "anthropic", "responses"])
@pytest.mark.parametrize("upstream_wire", ["openai", "anthropic"])
async def test_default_orchestration_preserves_limits_in_both_http_requests(inbound_wire, upstream_wire):
    parsers = {
        "openai": to_llm_request_openai,
        "anthropic": to_llm_request_anthropic,
        "responses": to_llm_request_responses,
    }
    body = {
        "temperature": 0.2,
        "max_output_tokens" if inbound_wire == "responses" else "max_tokens": 2048,
    }
    if inbound_wire == "responses":
        body["input"] = "task"
    else:
        body["messages"] = [{"role": "user", "content": "task"}]
    if inbound_wire == "anthropic":
        body["stop_sequences"] = ["END"]
    if inbound_wire == "openai":
        body.update(stop=["END"], seed=42, response_format={"type": "json_object"})
    request = parsers[inbound_wire](body)
    captured = []

    def handle(req):
        captured.append(json.loads(req.content))
        if upstream_wire == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "answer"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    ref = {"model": "test", "wire_format": upstream_wire, "base_url": "http://upstream/v1"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        strategy = build_strategy({"strategy": "always", "tool": ref, "thinking": ref}, client=client)
        await strategy.run(request)
    assert len(captured) == 2
    for rendered in captured:
        assert rendered["max_tokens"] == 2048
        assert rendered["temperature"] == 0.2
        assert "chat_template_kwargs" not in rendered
        if upstream_wire == "anthropic":
            if inbound_wire != "responses":
                assert rendered["stop_sequences"] == ["END"]
            assert "stop" not in rendered
            assert "seed" not in rendered
            assert "response_format" not in rendered
        else:
            if inbound_wire != "responses":
                assert rendered["stop"] == ["END"]
            if inbound_wire == "openai":
                assert rendered["seed"] == 42

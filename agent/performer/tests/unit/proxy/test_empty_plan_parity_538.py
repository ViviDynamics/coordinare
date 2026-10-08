from __future__ import annotations

import json

import pytest

from performer.proxy.assembler import SseStreamWriter, assemble_sse
from performer.proxy.llm_turn import LLMRequest, LLMResponse, Message, StreamDelta
from performer.proxy.strategies import AlwaysThinkThenAct, OrchestrationRecord, SingleStrategy, act


class Upstream:
    name = "test"

    def __init__(self, response):
        self.response = response

    async def complete(self, request, *, tools_enabled=True):
        return self.response

    async def complete_stream(self, request, sink, *, tools_enabled=True):
        sink.delta(StreamDelta(kind="reasoning", text=self.response.reasoning or ""))
        for fragment in ("hel", "lo"):
            sink.delta(StreamDelta(kind="text", text=fragment))
        return self.response


def visible_semantics(blocks, wire):
    text, reasoning = [], []
    for block in blocks:
        for line in block.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            event = json.loads(line[6:])
            if wire == "openai":
                delta = event["choices"][0]["delta"]
                text.append(delta.get("content", ""))
                reasoning.append(delta.get("reasoning_content", ""))
            elif wire == "anthropic" and event["type"] == "content_block_delta":
                text.append(event["delta"].get("text", ""))
                reasoning.append(event["delta"].get("thinking", ""))
            elif wire == "responses":
                if event["type"] == "response.output_text.delta":
                    text.append(event["delta"])
                if event["type"] == "response.output_item.added" and event["item"]["type"] == "reasoning":
                    reasoning.extend(part["text"] for part in event["item"]["summary"])
    return "".join(text), "".join(reasoning)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["", "planner plan"])
@pytest.mark.parametrize("wire", ["openai", "anthropic", "responses"])
@pytest.mark.parametrize("exposure", ["thinking", "prepend_content", "drop"])
async def test_planner_is_the_only_visible_reasoning_in_both_paths(plan, wire, exposure):
    strategy = AlwaysThinkThenAct(
        thinking=Upstream(LLMResponse(content=plan)),
        tool=Upstream(LLMResponse(content="hello", reasoning="executor reasoning")),
    )
    request = LLMRequest(messages=(Message.user("task"),))
    buffered, _ = await strategy.run(request)
    writer = SseStreamWriter(wire_format=wire, expose_plan_as=exposure)
    streamed, _ = await strategy.run(request, act_sink=writer)
    writer.finish(streamed)
    assert visible_semantics(writer.blocks, wire) == visible_semantics(
        assemble_sse(buffered, wire_format=wire, expose_plan_as=exposure), wire,
    )
    assert buffered.reasoning == plan
    assert streamed.reasoning == plan


@pytest.mark.asyncio
async def test_single_strategy_preserves_executor_reasoning():
    response, _ = await SingleStrategy(
        tool=Upstream(LLMResponse(content="hello", reasoning="executor reasoning")),
    ).run(LLMRequest(messages=(Message.user("task"),)))
    assert response.reasoning == "executor reasoning"


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["openai", "anthropic", "responses"])
@pytest.mark.parametrize("exposure", ["thinking", "prepend_content", "drop"])
async def test_act_without_plan_keeps_buffered_and_streamed_reasoning_empty(wire, exposure):
    upstream = Upstream(LLMResponse(content="hello", reasoning="executor reasoning"))
    request = LLMRequest(messages=(Message.user("task"),))
    buffered = await act(upstream, request, None, OrchestrationRecord(strategy="conditional"))
    writer = SseStreamWriter(wire_format=wire, expose_plan_as=exposure)
    streamed = await act(upstream, request, None, OrchestrationRecord(strategy="conditional"), sink=writer)
    writer.finish(streamed)
    assert buffered.reasoning is None
    assert streamed.reasoning is None
    assert visible_semantics(writer.blocks, wire) == visible_semantics(
        assemble_sse(buffered, wire_format=wire, expose_plan_as=exposure), wire,
    ) == ("hello", "")

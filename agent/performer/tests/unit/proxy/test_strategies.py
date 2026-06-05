"""080 — orchestration strategy logic (think/act, conditional, think_once).

Uses fake upstreams so the planner/executor orchestration is tested in isolation
(no httpx, no aiohttp). Pins FR-012 (plan injected as a system message ahead of
the conversation), FR-015 (classifier gating + failure→think), FR-016 (think_once
caching + error-marker invalidation), and FR-017 (on_think_error fallback).
"""

from __future__ import annotations

import pytest

from performer.proxy.llm_turn import LLMRequest, LLMResponse, Message, ToolCall
from performer.proxy.strategies import (
    AlwaysThinkThenAct,
    ConditionalEscalation,
    SingleStrategy,
    StageState,
    ThinkOnceActMany,
)
from performer.proxy.upstreams import UpstreamError


class FakeUpstream:
    def __init__(self, name, *, response=None, raises=False, reasoning=None):
        self.name = name
        self._response = response
        self._raises = raises
        self._reasoning = reasoning
        self.calls: list[tuple[LLMRequest, bool]] = []

    async def complete(self, request: LLMRequest, *, tools_enabled: bool = True) -> LLMResponse:
        self.calls.append((request, tools_enabled))
        if self._raises:
            raise UpstreamError(f"{self.name} boom")
        if self._reasoning is not None:
            return LLMResponse(reasoning=self._reasoning)
        return self._response or LLMResponse(content=f"{self.name}-out")


def _req(*messages: Message, tools=()) -> LLMRequest:
    return LLMRequest(messages=messages or (Message.user("do it"),), tools=tools)


# --- always: think → act ---------------------------------------------------


@pytest.mark.asyncio
async def test_always_thinks_then_acts_with_plan_injected():
    thinking = FakeUpstream("think", reasoning="STEP 1: read file")
    tool = FakeUpstream("tool", response=LLMResponse(tool_calls=(ToolCall("c1", "read", {"p": "x"}),)))
    strat = AlwaysThinkThenAct(thinking=thinking, tool=tool)

    resp, rec = await strat.run(_req(Message.user("fix the bug")))

    # think called with tools hidden
    assert thinking.calls[0][1] is False
    # act called with tools enabled
    assert tool.calls[0][1] is True
    # plan injected as the FIRST message, a system role, ahead of the user turn
    act_req = tool.calls[0][0]
    assert act_req.messages[0].role == "system"
    assert "STEP 1: read file" in act_req.messages[0].content
    assert act_req.messages[1].role == "user"
    # tool_calls come from the tool model; plan surfaced as reasoning
    assert resp.tool_calls[0].name == "read"
    assert rec.plan == "STEP 1: read file"
    assert rec.strategy == "always"


@pytest.mark.asyncio
async def test_always_falls_back_to_act_on_think_error():
    thinking = FakeUpstream("think", raises=True)
    tool = FakeUpstream("tool", response=LLMResponse(content="done"))
    strat = AlwaysThinkThenAct(thinking=thinking, tool=tool, on_think_error="fall_back_to_act")

    resp, rec = await strat.run(_req())

    assert resp.content == "done"
    # act ran without a plan (no system message injected)
    assert tool.calls[0][0].messages[0].role != "system"
    assert any(c["phase"] == "think" and c["ok"] is False for c in rec.calls)


@pytest.mark.asyncio
async def test_always_raises_on_think_error_when_fail_policy():
    thinking = FakeUpstream("think", raises=True)
    tool = FakeUpstream("tool")
    strat = AlwaysThinkThenAct(thinking=thinking, tool=tool, on_think_error="fail")
    with pytest.raises(UpstreamError):
        await strat.run(_req())


# --- single ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_calls_only_tool_with_tools_enabled():
    tool = FakeUpstream("tool", response=LLMResponse(content="ok"))
    resp, rec = await SingleStrategy(tool=tool).run(_req())
    assert resp.content == "ok"
    assert len(tool.calls) == 1 and tool.calls[0][1] is True
    assert rec.strategy == "single"


# --- conditional -----------------------------------------------------------


class FakeClassifier:
    def __init__(self, score):
        self._score = score

    async def score(self, request):
        return self._score


@pytest.mark.asyncio
async def test_conditional_escalates_at_or_above_threshold():
    thinking = FakeUpstream("think", reasoning="plan")
    tool = FakeUpstream("tool")
    strat = ConditionalEscalation(
        thinking=thinking, tool=tool, classifier=FakeClassifier(0.8), threshold=0.6
    )
    _, rec = await strat.run(_req())
    assert rec.decision == "escalated"
    assert len(thinking.calls) == 1  # thought


@pytest.mark.asyncio
async def test_conditional_act_only_below_threshold():
    thinking = FakeUpstream("think", reasoning="plan")
    tool = FakeUpstream("tool")
    strat = ConditionalEscalation(
        thinking=thinking, tool=tool, classifier=FakeClassifier(0.2), threshold=0.6
    )
    _, rec = await strat.run(_req())
    assert rec.decision == "act_only"
    assert len(thinking.calls) == 0  # did NOT think
    assert tool.calls[0][0].messages[0].role != "system"  # no plan injected


# --- think_once ------------------------------------------------------------


@pytest.mark.asyncio
async def test_think_once_caches_plan_across_turns():
    thinking = FakeUpstream("think", reasoning="the plan")
    tool = FakeUpstream("tool")
    state = StageState(invalidate_after_turns=5)
    strat = ThinkOnceActMany(thinking=thinking, tool=tool, state=state)

    await strat.run(_req())
    await strat.run(_req())
    await strat.run(_req())

    assert len(thinking.calls) == 1  # thought once
    assert state.cached_plan == "the plan"


@pytest.mark.asyncio
async def test_think_once_replans_after_turn_budget():
    thinking = FakeUpstream("think", reasoning="p")
    tool = FakeUpstream("tool")
    state = StageState(invalidate_after_turns=2)
    strat = ThinkOnceActMany(thinking=thinking, tool=tool, state=state)
    for _ in range(3):
        await strat.run(_req())
    assert len(thinking.calls) == 2  # replanned once after 2 turns


@pytest.mark.asyncio
async def test_think_once_replans_on_error_marker():
    thinking = FakeUpstream("think", reasoning="p")
    tool = FakeUpstream("tool")
    state = StageState(invalidate_on_error=True)
    strat = ThinkOnceActMany(thinking=thinking, tool=tool, state=state)

    await strat.run(_req())  # plans (turn 1)
    # next turn carries a tool result with an error → replan
    await strat.run(_req(Message(role="tool", content="Traceback: boom", tool_call_id="c1")))
    assert len(thinking.calls) == 2


@pytest.mark.asyncio
async def test_think_once_replans_on_explicit_is_error_flag():
    """Error marker also fires on an explicit is_error tool result (no regex match)."""
    thinking = FakeUpstream("think", reasoning="p")
    tool = FakeUpstream("tool")
    state = StageState(invalidate_on_error=True)
    strat = ThinkOnceActMany(thinking=thinking, tool=tool, state=state)

    await strat.run(_req())  # plans
    # content has no error keyword, but the flag is set → replan
    await strat.run(_req(Message(role="tool", content="status 0 all good", tool_call_id="c1", _is_error=True)))
    assert len(thinking.calls) == 2

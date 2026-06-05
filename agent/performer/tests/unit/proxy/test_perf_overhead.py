"""080 — proxy non-model overhead budget (T033 / SC-003 / Principle IV).

Asserts the DualModelProxy's per-turn overhead EXCLUDING upstream model time
(inbound parse → strategy dispatch → assemble) stays well under the 50 ms
budget. Uses an instant fake upstream so the measured time is pure proxy glue.
"""

from __future__ import annotations

import time

import pytest

from performer.proxy.dual_model_proxy import run_completion
from performer.proxy.llm_turn import LLMResponse, ToolCall
from performer.proxy.strategies import AlwaysThinkThenAct, OrchestrationRecord

_OVERHEAD_BUDGET_MS = 50.0


class _InstantUpstream:
    name = "instant"

    async def complete(self, request, *, tools_enabled=True):
        if tools_enabled:
            return LLMResponse(content="done", tool_calls=(ToolCall("c1", "edit", {}),))
        return LLMResponse(reasoning="plan")


def _big_body(n_messages: int = 40) -> dict:
    return {
        "model": "x",
        "messages": [{"role": "user" if i % 2 else "assistant", "content": "lorem ipsum " * 20}
                     for i in range(n_messages)],
        "tools": [{"type": "function", "function": {"name": f"t{i}", "parameters": {}}} for i in range(8)],
    }


@pytest.mark.asyncio
async def test_proxy_overhead_under_budget():
    strat = AlwaysThinkThenAct(thinking=_InstantUpstream(), tool=_InstantUpstream())
    body = _big_body()

    # warm up (import/JIT-ish), then measure median of several runs
    await run_completion(body, "openai", strat, "thinking")
    samples = []
    for _ in range(20):
        t0 = time.perf_counter()
        await run_completion(body, "openai", strat, "thinking")
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    median_ms = samples[len(samples) // 2]
    assert median_ms < _OVERHEAD_BUDGET_MS, f"proxy overhead {median_ms:.2f}ms exceeds {_OVERHEAD_BUDGET_MS}ms"


@pytest.mark.asyncio
async def test_assemble_and_parse_roundtrip_is_cheap_for_anthropic():
    strat = AlwaysThinkThenAct(thinking=_InstantUpstream(), tool=_InstantUpstream())
    body = {"model": "x", "system": "s", "messages": [{"role": "user", "content": "go"}]}
    t0 = time.perf_counter()
    await run_completion(body, "anthropic", strat, "thinking")
    assert (time.perf_counter() - t0) * 1000.0 < _OVERHEAD_BUDGET_MS


def test_orchestration_record_is_lightweight():
    # observability record holds no bodies/secrets — just decision + call metadata
    rec = OrchestrationRecord(strategy="always", decision="think_then_act", plan="p")
    rec.record_call("think", ok=True, latency_ms=12.3)
    assert rec.calls == [{"phase": "think", "ok": True, "latency_ms": 12.3}]

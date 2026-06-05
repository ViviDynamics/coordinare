"""080 — DualModelProxy: strategy factory + end-to-end run_completion + aiohttp shell (T022/T026)."""

from __future__ import annotations

import httpx
import pytest

from performer.proxy.dual_model_proxy import (
    DualModelProxy,
    build_strategy,
    run_completion,
)
from performer.proxy.strategies import (
    AlwaysThinkThenAct,
    ConditionalEscalation,
    SingleStrategy,
    ThinkOnceActMany,
)

_ALWAYS_CFG = {
    "strategy": "always",
    "tool": {"name": "tool", "model": "qwen", "wire_format": "openai", "base_url": "http://spark/v1"},
    "thinking": {"name": "think", "model": "gptoss", "wire_format": "openai", "base_url": "http://spark/v1"},
    "expose_plan_as": "thinking",
}


# --- build_strategy --------------------------------------------------------


def test_build_strategy_dispatches_by_strategy():
    assert isinstance(build_strategy(_ALWAYS_CFG), AlwaysThinkThenAct)
    assert isinstance(build_strategy({**_ALWAYS_CFG, "strategy": "single"}), SingleStrategy)
    cond = {**_ALWAYS_CFG, "strategy": "conditional",
            "classifier": _ALWAYS_CFG["thinking"], "threshold": 0.5}
    assert isinstance(build_strategy(cond), ConditionalEscalation)
    to = {**_ALWAYS_CFG, "strategy": "think_once", "invalidate_after_turns": 3}
    assert isinstance(build_strategy(to), ThinkOnceActMany)


def test_build_strategy_rejects_unknown():
    with pytest.raises(ValueError, match="unknown orchestration strategy"):
        build_strategy({"strategy": "bogus", "tool": _ALWAYS_CFG["tool"]})


# --- run_completion end-to-end (fake strategy) -----------------------------


class _FakeStrategy:
    name = "always"

    async def run(self, request):
        from performer.proxy.strategies import OrchestrationRecord
        from performer.proxy.llm_turn import LLMResponse, ToolCall

        # echo: confirm the inbound request parsed (system + user present)
        assert request.messages[0].role in ("system", "user")
        return (
            LLMResponse(content="answer", tool_calls=(ToolCall("c1", "read", {"p": "x"}),), reasoning="PLAN"),
            OrchestrationRecord(strategy="always"),
        )


@pytest.mark.asyncio
async def test_run_completion_openai_end_to_end():
    body = {"model": "x", "messages": [{"role": "user", "content": "go"}],
            "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}]}
    out = await run_completion(body, "openai", _FakeStrategy(), "thinking")
    msg = out["choices"][0]["message"]
    assert msg["reasoning_content"] == "PLAN"
    assert msg["tool_calls"][0]["function"]["name"] == "read"


@pytest.mark.asyncio
async def test_run_completion_anthropic_end_to_end():
    body = {"model": "x", "system": "be terse", "messages": [{"role": "user", "content": "go"}]}
    out = await run_completion(body, "anthropic", _FakeStrategy(), "thinking")
    assert out["content"][0] == {"type": "thinking", "thinking": "PLAN"}


# --- aiohttp shell: real always-strategy through a mocked upstream ----------


@pytest.mark.asyncio
async def test_proxy_server_always_flow_over_http():
    # one mock transport answers both think and act upstream calls
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        if '"tools"' not in body:
            # think phase (tools hidden) → return a plan
            return httpx.Response(200, json={"choices": [{"message": {"content": "THE PLAN"}}]})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "done", "tool_calls": [{"id": "c1", "function": {"name": "edit", "arguments": "{}"}}]}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # inject the shared mock client; start() builds the strategy with it.
    proxy = DualModelProxy(orchestration=_ALWAYS_CFG, expose_plan_as="thinking", client=client)
    base = await proxy.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/v1/chat/completions",
                             json={"model": "x", "messages": [{"role": "user", "content": "go"}],
                                   "tools": [{"type": "function", "function": {"name": "edit", "parameters": {}}}]})
            assert r.status_code == 200
            msg = r.json()["choices"][0]["message"]
            assert msg["tool_calls"][0]["function"]["name"] == "edit"
            assert msg["reasoning_content"] == "THE PLAN"
    finally:
        await proxy.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_proxy_creates_and_owns_one_client_when_none_injected():
    """No injected client → the proxy builds one shared client for its lifetime
    and closes it on stop() (review finding: reuse, not one-per-call)."""
    proxy = DualModelProxy(orchestration={**_ALWAYS_CFG, "strategy": "single"})
    await proxy.start()
    assert proxy._owns_client is True
    created = proxy.client
    assert created is not None and created.is_closed is False
    await proxy.stop()
    assert created.is_closed is True
    assert proxy.client is None


@pytest.mark.asyncio
async def test_proxy_does_not_close_injected_client():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})))
    proxy = DualModelProxy(orchestration={**_ALWAYS_CFG, "strategy": "single"}, client=client)
    await proxy.start()
    assert proxy._owns_client is False
    await proxy.stop()
    assert client.is_closed is False  # caller owns it
    await client.aclose()

"""082 US9 / FR-010 — DualModelProxy front-door path acceptance.

The proxy is the in-container reverse proxy that non-claude_code CLIs point their
provider base URL at. Several backends call the OpenAI/Anthropic endpoints WITHOUT
the ``/v1`` prefix (opencode/openclaw → ``POST /chat/completions``; an
Anthropic-style CLI → ``POST /messages``). The original ``_PATH_WIRE`` only
registered ``/v1/...`` routes, so those requests hit aiohttp's 404 and dual-model
silently failed for every backend except claude_code.

These tests pin the contract: the front door MUST accept both the ``/v1``-prefixed
and bare forms, routing each to the correct wire format and returning 200 (not 404).
The upstream model call is stubbed with an injected httpx MockTransport client so no
network is touched; the proxy server itself is real aiohttp on loopback.
"""
from __future__ import annotations

import httpx
from performer.proxy.dual_model_proxy import DualModelProxy


def _mock_client() -> httpx.AsyncClient:
    """An httpx client whose every upstream call returns a canned OpenAI completion.

    The proxy uses this client for its internal think/act upstream calls; the test's
    own request to the proxy goes over real loopback TCP to the aiohttp server.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ]
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _single_orchestration() -> dict:
    return {
        "strategy": "single",
        "tool": {
            "name": "tool",
            "model": "test-model",
            "wire_format": "openai",
            "base_url": "http://upstream.local",
        },
    }


async def _post(base: str, path: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(
            f"{base}{path}",
            json={"messages": [{"role": "user", "content": "hi"}]},
            timeout=10.0,
        )


async def test_front_door_accepts_bare_chat_completions() -> None:
    """``POST /chat/completions`` (no ``/v1``) must route to openai wire, not 404."""
    proxy = DualModelProxy(orchestration=_single_orchestration(), client=_mock_client())
    base = await proxy.start()
    try:
        resp = await _post(base, "/chat/completions")
    finally:
        await proxy.stop()
    assert resp.status_code == 200, resp.text


async def test_front_door_accepts_bare_messages() -> None:
    """``POST /messages`` (no ``/v1``) must route to anthropic wire, not 404."""
    proxy = DualModelProxy(orchestration=_single_orchestration(), client=_mock_client())
    base = await proxy.start()
    try:
        resp = await _post(base, "/messages")
    finally:
        await proxy.stop()
    assert resp.status_code == 200, resp.text


async def _post_responses(base: str, path: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(
            f"{base}{path}",
            json={"instructions": "be terse", "input": "hi"},
            timeout=10.0,
        )


async def test_front_door_accepts_responses_paths() -> None:
    """codex 0.137.0 POSTs to ``/responses`` regardless of config (FR-011); both the
    bare and ``/v1``-prefixed forms must route to the responses wire, not 404."""
    proxy = DualModelProxy(orchestration=_single_orchestration(), client=_mock_client())
    base = await proxy.start()
    try:
        bare = await _post_responses(base, "/responses")
        v1 = await _post_responses(base, "/v1/responses")
    finally:
        await proxy.stop()
    assert bare.status_code == 200, bare.text
    assert v1.status_code == 200, v1.text
    assert bare.json()["object"] == "response"


async def test_front_door_still_accepts_v1_prefixed_paths() -> None:
    """The original ``/v1``-prefixed routes must keep working (no regression)."""
    proxy = DualModelProxy(orchestration=_single_orchestration(), client=_mock_client())
    base = await proxy.start()
    try:
        oai = await _post(base, "/v1/chat/completions")
        anth = await _post(base, "/v1/messages")
    finally:
        await proxy.stop()
    assert oai.status_code == 200, oai.text
    assert anth.status_code == 200, anth.text

"""084 US1 — startup health gating for the ``translate`` strategy (T008).

TDD: written BEFORE the translate branch in ``health.py`` exists and must FAIL
first. Covers FR-010 / quickstart S6: the translate health probe sends a
representative Anthropic body through the request translator, forwards it to the
OpenAI-wire upstream, runs the reply back through the declared normalizers + the
response translator, and gates on whether a valid structured Anthropic
``tool_use`` output survived the full round trip:

* healthy upstream (structured tool call survives translation) → ``proceed``
* untranslatable reply (no structured ``tool_use`` after normalize+translate) →
  ``fail_closed`` (or ``rerouted`` when ``reroute_upstream`` is set)
* the round trip exercises the declared normalizers: a harmony-leaked reply with
  ``harmony_tool_calls`` declared reassembles → healthy; with no normalizer the
  leak survives → unhealthy
* it MUST NOT fail open — an unhealthy translate target NEVER ``proceed``s
* non-200 and a wedged (timeout) upstream surface as ``unhealthy``

Exercised with an injected ``httpx`` client backed by ``MockTransport`` so the
full translate round trip is testable with no real network (Constitution II).
"""

from __future__ import annotations

import json

import httpx
import pytest

from performer.proxy.health import check_health
from performer.proxy.routing import TargetDescriptor

from .fixtures import HARMONY_LEAKED_JSON


def _target(
    *,
    normalizers: list[str] | None = None,
    reroute_upstream: str | None = None,
    base_url: str = "http://ollama:11434",
    upstream_model: str | None = None,
) -> TargetDescriptor:
    """A ``translate`` target (wire_format must be ``openai`` per Rule T1)."""
    return TargetDescriptor(
        base_url=base_url,
        wire_format="openai",
        strategy="translate",
        normalizers=normalizers or [],
        reroute_upstream=reroute_upstream,
        upstream_model=upstream_model,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _structured_toolcall_response(request: httpx.Request) -> httpx.Response:
    """A well-behaved OpenAI-wire upstream that emits a structured tool call."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-probe",
            "model": "gpt-oss:120b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_probe_0",
                                "type": "function",
                                "function": {"name": "ping", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
    )


# --- healthy round trip ----------------------------------------------------- #


@pytest.mark.asyncio
async def test_translate_healthy_probe_proceeds():
    """A 200 whose structured tool call survives normalize+translate → proceed."""
    target = _target()
    async with _client(_structured_toolcall_response) as client:
        result = await check_health(target, client=client)
    assert result.status == "healthy"
    assert result.resolved_action == "proceed"


@pytest.mark.asyncio
async def test_translate_probe_forwards_translated_openai_body_with_tools():
    """The probe routes a representative Anthropic body THROUGH the request
    translator (OpenAI-wire ``messages``) and the upstream is actually asked to
    call a tool (``tools`` present) — otherwise no structured call could come
    back and the probe would be meaningless (quickstart S6)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return _structured_toolcall_response(request)

    target = _target()
    async with _client(handler) as client:
        await check_health(target, client=client)

    body = seen["body"]
    # OpenAI wire, not Anthropic: chat ``messages``, and tools so the upstream is
    # asked to emit a structured call.
    assert isinstance(body.get("messages"), list) and body["messages"]
    assert body.get("tools")
    # forwarded to the OpenAI chat-completions endpoint, not /v1/messages
    assert seen["url"].endswith("/v1/chat/completions")


# --- 084 Ollama-compat: probe URL double-/v1 + upstream_model probe body ----- #


@pytest.mark.asyncio
async def test_translate_probe_avoids_double_v1_when_base_url_ends_in_v1():
    """When ``base_url`` ends in ``/v1`` (Ollama-direct config), the probe URL
    must be ``…/v1/chat/completions`` — NOT ``…/v1/v1/chat/completions`` (404)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _structured_toolcall_response(request)

    target = _target(base_url="http://ollama:11434/v1")
    async with _client(handler) as client:
        await check_health(target, client=client)

    assert seen["url"].endswith("/v1/chat/completions")
    assert "/v1/v1/" not in seen["url"]


@pytest.mark.asyncio
async def test_translate_probe_body_uses_upstream_model_when_declared():
    """When the target declares an ``upstream_model``, the probe body addresses
    the upstream's real model name (not the routing-key model the CLI uses), so
    Ollama doesn't 404 the probe and gate every routed path unhealthy."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _structured_toolcall_response(request)

    target = _target(upstream_model="gpt-oss:120b")
    async with _client(handler) as client:
        await check_health(target, client=client, model="claude-sonnet-4-5")

    assert seen["body"]["model"] == "gpt-oss:120b"


# --- untranslatable reply gates closed / reroutes --------------------------- #


@pytest.mark.asyncio
async def test_translate_untranslatable_reply_fails_closed():
    """A 200 with only plain text (no structured tool call after translation)
    and no reroute_upstream → fail_closed (never fail-open)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}
                ]
            },
        )

    target = _target(reroute_upstream=None)
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"
    assert result.reason


@pytest.mark.asyncio
async def test_translate_untranslatable_reply_with_reroute_reroutes():
    """Same untranslatable reply but with a declared reroute_upstream → rerouted
    (the Ollama-direct fallback), not fail-open."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}
                ]
            },
        )

    target = _target(reroute_upstream="http://ollama-clean:11434")
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "unhealthy"
    assert result.resolved_action == "rerouted"


# --- the round trip runs the declared normalizers --------------------------- #


@pytest.mark.asyncio
async def test_translate_harmony_leak_with_normalizer_is_healthy():
    """A harmony-leaked reply (LiteLLM #17246 shape) is reassembled by the
    declared ``harmony_tool_calls`` normalizer before translation, so a
    structured tool_use survives → healthy. This proves the probe runs the full
    normalize→translate round trip, not just a raw upstream check."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json.loads(json.dumps(HARMONY_LEAKED_JSON)))

    target = _target(normalizers=["harmony_tool_calls"])
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "healthy"
    assert result.resolved_action == "proceed"


@pytest.mark.asyncio
async def test_translate_harmony_leak_without_normalizer_fails_closed():
    """The SAME harmony-leaked reply with NO normalizer declared keeps the leak
    in ``content`` — no structured tool_use survives → unhealthy → fail_closed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json.loads(json.dumps(HARMONY_LEAKED_JSON)))

    target = _target(normalizers=[], reroute_upstream=None)
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"


# --- never fail open -------------------------------------------------------- #


@pytest.mark.asyncio
async def test_translate_non_200_is_unhealthy():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "model loading"})

    target = _target(reroute_upstream=None)
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"


@pytest.mark.asyncio
async def test_translate_probe_timeout_is_unhealthy_not_a_hang():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("wedged upstream", request=request)

    target = _target(reroute_upstream=None)
    async with _client(handler) as client:
        result = await check_health(target, client=client, timeout=0.01)
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"
    assert "timed out" in result.reason


@pytest.mark.asyncio
async def test_translate_never_fails_open_on_unhealthy():
    """An unhealthy translate target NEVER resolves to ``proceed`` (FR-010)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    for reroute in (None, "http://ollama-clean:11434"):
        target = _target(reroute_upstream=reroute)
        async with _client(handler) as client:
            result = await check_health(target, client=client)
        assert result.resolved_action != "proceed"

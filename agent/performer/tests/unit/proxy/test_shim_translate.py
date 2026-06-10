"""084 US1 — SelfHostedShim end-to-end translate wiring (T012).

The shim, when its target's ``strategy == "translate"``, makes a ``claude_code``
(Anthropic-wire) CLI reach an OpenAI-wire upstream (Ollama-direct gpt-oss) with
no LiteLLM in the path:

* the inbound Anthropic ``/v1/messages`` body is run through ``translate_request``
  and forwarded to the upstream's OpenAI ``/v1/chat/completions`` path;
* the upstream OpenAI reply is run through the declared normalizers and then
  ``translate_response`` (the OUTERMOST step, Decision 4) so the CLI sees a valid
  Anthropic message object — non-streaming JSON and streaming SSE;
* a non-2xx upstream is surfaced verbatim and the translator is NOT invoked
  (FR-012) — translating an error envelope would mask the real failure.

Driven through the real aiohttp server with an injected ``httpx`` MockTransport
upstream, so the full forward path is exercised with no real network.
"""

from __future__ import annotations

import json

import httpx
import pytest
from structlog.testing import capture_logs

from performer.proxy.normalizers.harmony import HarmonyToolCallsNormalizer
from performer.proxy.routing import TargetDescriptor
from performer.proxy.shim import SelfHostedShim

from .fixtures import (
    HARMONY_EXPECTED_TOOL_NAME,
    HARMONY_LEAKED_JSON,
    STUB_OPENAI_ERROR_BODY,
    STUB_OPENAI_ERROR_STATUS,
    STUB_OPENAI_NONSTREAM_JSON,
    STUB_OPENAI_STREAM_SSE_CHUNKS,
)


def _target(
    base_url: str = "http://ollama:11434", *, upstream_model: str | None = None
) -> TargetDescriptor:
    return TargetDescriptor(
        base_url=base_url,
        wire_format="openai",
        strategy="translate",
        upstream_model=upstream_model,
    )


def _anthropic_request(*, stream: bool = False) -> dict:
    return {
        "model": "claude-3-5-sonnet",
        "max_tokens": 256,
        "system": "You are a helpful assistant.",
        "messages": [{"role": "user", "content": "Summarize the README."}],
        "stream": stream,
    }


# --- non-streaming round trip ---------------------------------------------- #


@pytest.mark.asyncio
async def test_nonstream_translates_request_and_response():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=dict(STUB_OPENAI_NONSTREAM_JSON))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(target=_target(), client=client)
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/v1/messages", json=_anthropic_request())
    finally:
        await shim.stop()
        await client.aclose()

    # Upstream saw an OpenAI body at the chat-completions path.
    assert seen["url"].endswith("/v1/chat/completions")
    assert isinstance(seen["body"]["messages"], list)
    assert seen["body"]["messages"][0]["role"] == "system"
    assert "max_tokens" in seen["body"]

    # CLI saw a valid Anthropic message object.
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    text = "".join(b["text"] for b in body["content"] if b["type"] == "text")
    assert text == "The README documents the build steps."
    assert body["stop_reason"] == "end_turn"
    # usage renamed openai → anthropic
    assert body["usage"]["input_tokens"] == 27
    assert body["usage"]["output_tokens"] == 9


@pytest.mark.asyncio
async def test_nonstream_composes_normalizer_before_translation():
    """A harmony-leaked OpenAI reply is reassembled by the declared
    ``harmony_tool_calls`` normalizer BEFORE wire translation, so the CLI sees a
    structured Anthropic ``tool_use`` block and zero harmony markers."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json.loads(json.dumps(HARMONY_LEAKED_JSON)))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(
        target=_target(), normalizers=[HarmonyToolCallsNormalizer()], client=client
    )
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/v1/messages", json=_anthropic_request())
    finally:
        await shim.stop()
        await client.aclose()

    body = r.json()
    tool_uses = [b for b in body["content"] if b.get("type") == "tool_use"]
    assert tool_uses and tool_uses[0]["name"] == HARMONY_EXPECTED_TOOL_NAME
    assert "<|channel|>" not in json.dumps(body)


# --- 084 Ollama-compat: upstream_model rewrite + double-/v1 avoidance -------- #


@pytest.mark.asyncio
async def test_nonstream_rewrites_model_to_upstream_model():
    """When the target declares an ``upstream_model``, the forwarded OpenAI body
    carries the upstream's model name — not the Anthropic name the CLI sent (which
    Ollama would 404)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=dict(STUB_OPENAI_NONSTREAM_JSON))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(
        target=_target(upstream_model="gpt-oss:120b"), client=client
    )
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{base}/v1/messages", json=_anthropic_request())
    finally:
        await shim.stop()
        await client.aclose()

    assert seen["body"]["model"] == "gpt-oss:120b"


@pytest.mark.asyncio
async def test_nonstream_keeps_cli_model_when_no_upstream_model():
    """With no ``upstream_model`` declared, the translated body keeps whatever
    model name the request translator produced (no rewrite)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=dict(STUB_OPENAI_NONSTREAM_JSON))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(target=_target(), client=client)
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{base}/v1/messages", json=_anthropic_request())
    finally:
        await shim.stop()
        await client.aclose()

    assert seen["body"]["model"] == "claude-3-5-sonnet"


@pytest.mark.asyncio
async def test_nonstream_avoids_double_v1_when_base_url_ends_in_v1():
    """When ``base_url`` already ends in ``/v1`` (e.g. an Ollama-direct config),
    the translated path must NOT produce ``…/v1/v1/chat/completions`` (Ollama
    404). The upstream sees exactly ``…/v1/chat/completions``."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=dict(STUB_OPENAI_NONSTREAM_JSON))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(target=_target("http://ollama:11434/v1"), client=client)
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{base}/v1/messages", json=_anthropic_request())
    finally:
        await shim.stop()
        await client.aclose()

    assert seen["url"].endswith("/v1/chat/completions")
    assert "/v1/v1/" not in seen["url"]


# --- streaming round trip --------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_translates_sse_to_anthropic_events():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"".join(STUB_OPENAI_STREAM_SSE_CHUNKS),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(target=_target(), client=client)
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base}/v1/messages", json=_anthropic_request(stream=True)
            )
    finally:
        await shim.stop()
        await client.aclose()

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = [
        line[len("event: "):]
        for line in r.text.splitlines()
        if line.startswith("event: ")
    ]
    assert events[0] == "message_start"
    assert events[-1] == "message_stop"
    assert "content_block_delta" in events


# --- FR-012 non-2xx verbatim, translator NOT invoked ------------------------ #


@pytest.mark.asyncio
async def test_nonstream_non_2xx_surfaced_verbatim_translator_not_invoked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            STUB_OPENAI_ERROR_STATUS,
            content=STUB_OPENAI_ERROR_BODY,
            headers={"content-type": "application/json"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(target=_target(), client=client)
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/v1/messages", json=_anthropic_request())
    finally:
        await shim.stop()
        await client.aclose()

    assert r.status_code == STUB_OPENAI_ERROR_STATUS
    # the upstream OpenAI error envelope is surfaced verbatim — NOT rewrapped as
    # an Anthropic ``type: "message"`` object
    body = r.json()
    assert body["error"]["message"] == "model gpt-oss:120b is loading"
    assert body.get("type") != "message"


# --- S8 / FR-011: no secrets or bodies in the observability log ------------- #


@pytest.mark.asyncio
async def test_request_log_carries_only_metadata_no_body_or_secret():
    """FR-011 / quickstart S8: the per-request log line records method, path,
    status, latency, which translator ran, and which normalizers ran — and
    NOTHING that could leak a body, token text, or auth secret."""
    secret_text = "SUPERSECRET-token-and-prompt-body-xyz"

    def handler(request: httpx.Request) -> httpx.Response:
        reply = dict(STUB_OPENAI_NONSTREAM_JSON)
        return httpx.Response(200, json=reply)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(
        target=_target(), normalizers=[HarmonyToolCallsNormalizer()], client=client
    )
    base = await shim.start()
    body = _anthropic_request()
    body["system"] = secret_text  # a body field that must never reach the log
    try:
        with capture_logs() as logs:
            async with httpx.AsyncClient() as c:
                await c.post(
                    f"{base}/v1/messages",
                    json=body,
                    headers={"authorization": f"Bearer {secret_text}"},
                )
    finally:
        await shim.stop()
        await client.aclose()

    entries = [e for e in logs if e.get("event") == "selfhosted_shim.request"]
    assert entries, "expected a selfhosted_shim.request log line"
    entry = entries[0]
    # exactly the safe metadata keys (plus structlog's own event/log_level)
    assert entry["method"] == "POST"
    assert entry["path"] == "/v1/messages"
    assert entry["status"] == 200
    assert entry["translate"] is True
    assert entry["normalizers"] == ["harmony_tool_calls"]
    # the secret/body text appears in NO value of the log record
    assert secret_text not in json.dumps(entry, default=str)

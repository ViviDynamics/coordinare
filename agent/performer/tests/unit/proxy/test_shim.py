"""078 US1 — SelfHostedShim normalizer application (T013, written FIRST / must FAIL).

The shim applies ONLY the target's explicitly-declared normalizers, in order, on
both the JSON and SSE paths; an unrecognized/unrelated response shape flows
through unchanged (fail-open, SC-006, FR-078-9). The same format-keyed normalizer
instance is reusable across two different backends (SC-006) — quirks are
per-format, not per-agent.
"""

from __future__ import annotations

import json

import httpx
import pytest

from performer.proxy.normalizers.harmony import HarmonyToolCallsNormalizer
from performer.proxy.normalizers.reasoning import StripReasoningNormalizer
from performer.proxy.routing import TargetDescriptor
from performer.proxy.shim import (
    _CONNECT_TIMEOUT,
    _DEFAULT_FORWARD_TIMEOUT,
    SelfHostedShim,
)
from tests.unit.proxy.fixtures import (
    HARMONY_EXPECTED_TOOL_NAME,
    HARMONY_LEAKED_JSON,
    REASONING_OPENAI_JSON,
)


def _target(base_url="http://upstream:11434"):
    # the shim only reads target.base_url for forwarding; strategy/normalizers on
    # the descriptor are irrelevant here (normalizers are injected directly).
    return TargetDescriptor(
        base_url=base_url, wire_format="openai", strategy="reroute"
    )


def _shim(normalizers):
    return SelfHostedShim(target=_target(), normalizers=normalizers)


def test_no_normalizers_is_byte_for_byte_passthrough():
    shim = _shim([])
    body = {"choices": [{"message": {"content": "untouched"}}]}
    assert shim.normalize_json(body) == body


def test_only_declared_normalizer_runs_harmony_only():
    """A shim declaring only harmony reassembles tool calls but does NOT touch a
    reasoning_content field (that normalizer was not declared)."""
    shim = _shim([HarmonyToolCallsNormalizer()])

    out = shim.normalize_json(HARMONY_LEAKED_JSON)
    assert out["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == (
        HARMONY_EXPECTED_TOOL_NAME
    )

    # reasoning_content is left intact because strip_reasoning was not declared
    out2 = shim.normalize_json(REASONING_OPENAI_JSON)
    assert "reasoning_content" in out2["choices"][0]["message"]


def test_only_declared_normalizer_runs_reasoning_only():
    shim = _shim([StripReasoningNormalizer()])
    out = shim.normalize_json(REASONING_OPENAI_JSON)
    assert "reasoning_content" not in out["choices"][0]["message"]

    # harmony leakage is left intact because harmony_tool_calls was not declared
    out2 = shim.normalize_json(HARMONY_LEAKED_JSON)
    assert "tool_calls" not in out2["choices"][0]["message"]
    assert "<|channel|>" in out2["choices"][0]["message"]["content"]


def test_unknown_format_passes_through_fail_open():
    """An unrelated response shape neither normalizer recognizes is returned
    unchanged rather than dropped (fail-open, FR-078-9)."""
    shim = _shim([HarmonyToolCallsNormalizer(), StripReasoningNormalizer()])
    weird = {"object": "something.else", "data": [{"k": "v"}], "content": "plain"}
    assert shim.normalize_json(weird) == weird


def test_normalizers_apply_in_declared_order():
    """Both declared normalizers run; harmony reassembles AND reasoning is stripped
    on a body carrying both quirks."""
    combined = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "reasoning_content": "let me think",
                    "content": (
                        "<|channel|>commentary to=functions.read_file "
                        '<|constrain|>json<|message|>{"path": "README.md"}<|call|>'
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }
    shim = _shim([HarmonyToolCallsNormalizer(), StripReasoningNormalizer()])
    out = shim.normalize_json(combined)
    msg = out["choices"][0]["message"]
    assert "reasoning_content" not in msg
    assert msg["tool_calls"][0]["function"]["name"] == HARMONY_EXPECTED_TOOL_NAME
    assert "<|channel|>" not in (msg.get("content") or "")


def test_shared_normalizer_instance_reused_across_two_backends():
    """One harmony normalizer instance serves two distinct backends (SC-006)."""
    shared = HarmonyToolCallsNormalizer()
    backend_a = SelfHostedShim(target=_target("http://a:11434"), normalizers=[shared])
    backend_b = SelfHostedShim(target=_target("http://b:11434"), normalizers=[shared])

    for shim in (backend_a, backend_b):
        out = shim.normalize_json(json.loads(json.dumps(HARMONY_LEAKED_JSON)))
        fn = out["choices"][0]["message"]["tool_calls"][0]["function"]
        assert fn["name"] == HARMONY_EXPECTED_TOOL_NAME


def test_sse_chain_composes_declared_filters():
    """sse_chain builds a fresh filter per declared normalizer; with none it is a
    pass-through identity chain."""
    passthrough = _shim([]).sse_chain()
    chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    assert passthrough.feed(chunk) + passthrough.flush() == chunk


# --- HTTP path: streaming status passthrough + forwarding-timeout bound -------


@pytest.mark.asyncio
async def test_streaming_non_200_surfaces_real_status_not_forced_200():
    """A non-200 upstream on the streaming path must surface the REAL status with
    the upstream's error body — never a 200 wrapping a garbled/empty stream (the
    077 'empty/garbage' masquerade this layer exists to eliminate)."""

    def handler(req: httpx.Request) -> httpx.Response:
        # The CLI asked to stream; the self-hosted upstream is overloaded (429).
        assert b'"stream"' in req.content
        return httpx.Response(
            429, json={"error": {"message": "upstream overloaded"}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(target=_target(), client=client)
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base}/v1/chat/completions",
                json={"model": "x", "messages": [], "stream": True},
            )
        assert r.status_code == 429
        assert r.json()["error"]["message"] == "upstream overloaded"
        # NOT an SSE body forced to 200
        assert "text/event-stream" not in r.headers.get("content-type", "")
    finally:
        await shim.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_streaming_200_passes_through_as_sse():
    """A healthy 200 stream is still proxied as an event-stream (the fix only
    diverts non-200s; the happy path is unchanged)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(target=_target(), client=client)
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base}/v1/chat/completions",
                json={"model": "x", "messages": [], "stream": True},
            )
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert b'"content":"hi"' in r.content
    finally:
        await shim.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_wedged_upstream_surfaces_clean_502():
    """A forwarding error (e.g. the 077 spark/qwen runner-wedge raising a read
    timeout) surfaces as a clean 502 — never a hang, never a leaked body."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream wedged", request=req)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(target=_target(), client=client)
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base}/v1/chat/completions",
                json={"model": "x", "messages": [], "stream": True},
            )
        assert r.status_code == 502
        # generic message only — no upstream body/token leakage (FR-078-10)
        assert r.json()["error"]["message"] == "self-hosted shim proxy failed"
    finally:
        await shim.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_owned_client_is_timeout_bounded():
    """When the shim owns its client (no injection), it is built with the
    per-request forwarding timeout so a wedged upstream cannot stall a card
    indefinitely. An injected client (tests) keeps its own configuration."""
    shim = SelfHostedShim(target=_target())  # no client injected → shim owns one
    await shim.start()
    try:
        assert shim._owns_client is True
        timeout = shim.client.timeout
        assert timeout.read == _DEFAULT_FORWARD_TIMEOUT
        assert timeout.connect == _CONNECT_TIMEOUT
    finally:
        await shim.stop()


@pytest.mark.asyncio
async def test_custom_forward_timeout_flows_to_owned_client():
    shim = SelfHostedShim(target=_target(), forward_timeout=42.0)
    await shim.start()
    try:
        assert shim.client.timeout.read == 42.0
    finally:
        await shim.stop()


# --- 122: front-door path canonicalization (no-/v1 CLIs) -------------------- #


@pytest.mark.asyncio
async def test_no_v1_chat_completions_is_routed_and_canonicalized():
    """An OpenAI-wire CLI whose repointed base_url lacks /v1 POSTs to
    ``/chat/completions`` (no /v1). The shim must route it (not 404 at the router)
    AND forward to the upstream's ``/v1/chat/completions`` (122: shared cause of the
    openclaw + opencode shim 404s)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(
        target=TargetDescriptor(
            base_url="https://litellm.example.com", wire_format="openai",
            strategy="observe",
        ),
        client=client,
    )
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/chat/completions",
                             json={"model": "spark/gpt-oss:120b", "messages": []})
        assert r.status_code == 200
    finally:
        await shim.stop()
        await client.aclose()
    assert seen["url"] == "https://litellm.example.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_v1_chat_completions_still_forwarded_unchanged():
    """The canonical /v1/chat/completions path (junie/hermes) is unaffected."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shim = SelfHostedShim(
        target=TargetDescriptor(
            base_url="http://192.168.3.30:11434", wire_format="openai",
            strategy="normalize", normalizers=["strip_control_chars"],
        ),
        client=client,
    )
    base = await shim.start()
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{base}/v1/chat/completions", json={"messages": []})
    finally:
        await shim.stop()
        await client.aclose()
    assert seen["url"] == "http://192.168.3.30:11434/v1/chat/completions"


# --- 122: upstream auth injection (junie/pi/hermes key-resolution gap) ------ #


def test_upstream_auth_env_overrides_forwarded_authorization(monkeypatch):
    """When the target names an upstream_auth_env, the shim replaces the CLI's
    Authorization with Bearer <env value> so a CLI that fails to put the key on the
    wire still authenticates against LiteLLM."""
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-real-key")
    shim = SelfHostedShim(
        target=TargetDescriptor(
            base_url="https://litellm.example.com", wire_format="openai",
            strategy="normalize", normalizers=["strip_control_chars"],
            upstream_auth_env="LITELLM_MASTER_KEY",
        ),
    )
    out = shim._forward_headers({"Authorization": "Bearer no-key-required",
                                "api-key": "no-key-required",
                                "Content-Type": "application/json"})
    assert out["Authorization"] == "Bearer sk-real-key"
    # alternate auth headers (api-key/x-api-key) are dropped so a stale one can't
    # shadow the injected Authorization at the upstream (hermes sends api-key).
    assert "api-key" not in {k.lower() for k in out}
    assert out["Content-Type"] == "application/json"


def test_upstream_auth_env_noop_when_unset_on_target():
    """No upstream_auth_env → the CLI's header passes through unchanged."""
    shim = SelfHostedShim(
        target=TargetDescriptor(
            base_url="http://ollama:11434", wire_format="openai", strategy="reroute",
        ),
    )
    out = shim._forward_headers({"Authorization": "Bearer cli-token"})
    assert out["Authorization"] == "Bearer cli-token"

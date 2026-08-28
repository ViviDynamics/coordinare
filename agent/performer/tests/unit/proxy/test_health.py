"""078 US3 — startup health / smoke-test gating (T023).

TDD: written BEFORE ``health.py`` exists and must FAIL first. Covers the
FR-078-5 gating state machine (SC-005):

* ``healthy`` → ``proceed`` (routing gated open)
* ``unhealthy`` + ``reroute_upstream`` declared → ``rerouted`` (auto-reroute)
* ``unhealthy`` + none → ``fail_closed`` (clear error, card not accepted)
* probe timeout → ``unhealthy`` (never hangs startup) then gated per the
  auto-reroute-then-fail-closed path — NEVER fail-open onto the unhealthy path.

The probe is exercised with an injected ``httpx`` client backed by
``MockTransport`` so the tool-calling path and the timeout edge are testable
with no real network.
"""

from __future__ import annotations

import httpx
import pytest

from performer.proxy.health import HealthResult, check_health, gate
from performer.proxy.routing import TargetDescriptor


def _target(
    *,
    strategy: str = "normalize",
    normalizers: list[str] | None = None,
    reroute_upstream: str | None = None,
    wire_format: str = "openai",
    base_url: str = "http://litellm:4000",
) -> TargetDescriptor:
    if normalizers is None:
        normalizers = ["harmony_tool_calls"] if strategy == "normalize" else []
    return TargetDescriptor(
        base_url=base_url,
        wire_format=wire_format,
        strategy=strategy,
        normalizers=normalizers,
        reroute_upstream=reroute_upstream,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- gate(): the pure decision (no probe, no network) ----------------------- #


def test_gate_healthy_proceeds():
    target = _target()
    result = gate(target, "healthy", None)
    assert isinstance(result, HealthResult)
    assert result.status == "healthy"
    assert result.resolved_action == "proceed"
    assert result.target is target


def test_gate_unhealthy_with_reroute_upstream_reroutes():
    target = _target(reroute_upstream="http://ollama:11434")
    result = gate(target, "unhealthy", "no tool_calls in probe response")
    assert result.status == "unhealthy"
    assert result.resolved_action == "rerouted"
    # reason carried through for observability
    assert result.reason == "no tool_calls in probe response"


def test_gate_unhealthy_without_reroute_fails_closed():
    target = _target(reroute_upstream=None)
    result = gate(target, "unhealthy", "probe timed out after 10.0s")
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"
    # the fail-closed error must carry a clear, populated reason
    assert result.reason
    assert "timed out" in result.reason


def test_gate_never_fails_open_on_unhealthy():
    """An unhealthy target NEVER resolves to ``proceed`` — that would be
    fail-open onto a known-broken path (FR-078-5)."""
    assert gate(_target(), "unhealthy", "x").resolved_action != "proceed"
    assert (
        gate(_target(reroute_upstream="http://ollama:11434"), "unhealthy", "x")
        .resolved_action
        != "proceed"
    )


# --- check_health(): probe + gate over an injected client ------------------- #


@pytest.mark.asyncio
async def test_healthy_probe_proceeds():
    """A 200 carrying structured tool_calls → healthy → proceed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_0",
                                    "type": "function",
                                    "function": {"name": "ping", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    target = _target()
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "healthy"
    assert result.resolved_action == "proceed"


@pytest.mark.asyncio
async def test_completion_probe_survives_unescaped_control_byte_via_raw_normalizer():
    """122: a flaky upstream (glm-4.7-flash via LiteLLM) intermittently emits an
    unescaped control byte in reasoning_content → the raw body is invalid JSON, so
    ``response.json()`` raises and the parsed normalize_json path can never run. When
    ``strip_control_chars`` is declared, the probe MUST apply its RAW pre-parse path
    first (mirroring the shim) so the body parses and the completion is judged."""
    # raw 200 body with a literal control byte (0x07 BELL) inside a string value
    bad = (
        '{"choices":[{"message":{"role":"assistant",'
        '"content":"ok","reasoning_content":"thinking\x07here"}}]}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bad.encode("utf-8"),
                              headers={"content-type": "application/json"})

    target = _target(strategy="normalize", normalizers=["strip_control_chars"])
    target = TargetDescriptor(
        base_url=target.base_url, wire_format="openai", strategy="normalize",
        normalizers=["strip_control_chars"], health_probe="completion",
    )
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "healthy"
    assert result.resolved_action == "proceed"


@pytest.mark.asyncio
async def test_completion_probe_invalid_json_without_raw_normalizer_is_unhealthy():
    """Control: the same broken body with NO raw-capable normalizer declared stays
    unhealthy (the fix is scoped to declared normalizers, not a blanket leniency)."""
    bad = '{"choices":[{"message":{"content":"ok\x07"}}]}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bad.encode("utf-8"),
                              headers={"content-type": "application/json"})

    target = TargetDescriptor(
        base_url="http://litellm:4000", wire_format="openai", strategy="observe",
        normalizers=[], health_probe="completion",
    )
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "unhealthy"


@pytest.mark.asyncio
async def test_harmony_leak_probe_is_unhealthy_and_reroutes():
    """The 077 openclaw failure: gpt-oss harmony leaks into content instead of
    structured tool_calls. The probe sees no tool_calls → unhealthy; with a
    declared reroute_upstream it auto-reroutes (the Ollama-direct fix)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "<|channel|>commentary to=functions.ping "
                                "<|constrain|>json<|message|>{}<|call|>"
                            ),
                        }
                    }
                ]
            },
        )

    target = _target(reroute_upstream="http://ollama:11434")
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "unhealthy"
    assert result.resolved_action == "rerouted"
    assert result.reason


@pytest.mark.asyncio
async def test_unhealthy_probe_without_reroute_fails_closed():
    """No tool_calls and no fallback upstream → fail-closed with a clear reason."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    target = _target(reroute_upstream=None)
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"
    assert result.reason


@pytest.mark.asyncio
async def test_probe_timeout_surfaces_unhealthy_not_a_hang():
    """A wedged upstream (the 077 local/qwen runner hang): the bounded probe
    raises a timeout, which surfaces as ``unhealthy`` rather than hanging
    startup. With no reroute_upstream it then fails closed (SC-005, Edge Case)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("wedged upstream", request=request)

    target = _target(reroute_upstream=None)
    async with _client(handler) as client:
        result = await check_health(target, client=client, timeout=0.01)
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"
    assert "timed out" in result.reason


@pytest.mark.asyncio
async def test_probe_timeout_with_reroute_upstream_reroutes():
    """A wedged upstream that DOES declare a clean fallback auto-reroutes
    instead of failing closed — timeout → unhealthy → rerouted."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("wedged upstream", request=request)

    target = _target(reroute_upstream="http://ollama:11434")
    async with _client(handler) as client:
        result = await check_health(target, client=client, timeout=0.01)
    assert result.status == "unhealthy"
    assert result.resolved_action == "rerouted"


@pytest.mark.asyncio
async def test_connect_error_is_unhealthy():
    """A target that refuses the connection (077 hermes can't-connect) is
    unhealthy, not a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    target = _target(reroute_upstream=None)
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"
    assert result.reason


@pytest.mark.asyncio
async def test_non_200_probe_is_unhealthy():
    """An upstream 5xx on the probe is unhealthy."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "model loading"})

    target = _target(reroute_upstream=None)
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"


@pytest.mark.asyncio
async def test_probe_body_carries_routed_model_not_placeholder():
    """The probe must address the REAL routed model, not a ``"probe"`` placeholder.

    Ollama-direct (and most OpenAI-compatible servers) validate the ``model``
    field and answer an unknown model with HTTP 404, which would gate every
    routed path unhealthy at startup. ``check_health`` therefore sends the model
    it is gating (``gpt-oss:120b``) so the probe reaches a model that exists.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_0",
                                    "type": "function",
                                    "function": {"name": "ping", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    target = _target(strategy="reroute", wire_format="openai")
    async with _client(handler) as client:
        result = await check_health(target, model="gpt-oss:120b", client=client)
    assert result.status == "healthy"
    assert seen["body"]["model"] == "gpt-oss:120b"


@pytest.mark.asyncio
async def test_translate_probe_body_carries_routed_model():
    """The translate path routes the Anthropic probe through ``translate_request``;
    the routed model must survive into the OpenAI body the upstream sees."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_0",
                                    "type": "function",
                                    "function": {"name": "ping", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    target = _target(strategy="translate", wire_format="openai", normalizers=[])
    async with _client(handler) as client:
        result = await check_health(target, model="gpt-oss:120b", client=client)
    assert result.status == "healthy"
    assert seen["body"]["model"] == "gpt-oss:120b"


@pytest.mark.asyncio
async def test_anthropic_tool_use_probe_is_healthy():
    """An anthropic-wire target is healthy when the probe returns a tool_use
    content block."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "tool_use", "id": "tu_0", "name": "ping", "input": {}}
                ],
                "stop_reason": "tool_use",
            },
        )

    target = _target(wire_format="anthropic")
    async with _client(handler) as client:
        result = await check_health(target, client=client)
    assert result.status == "healthy"
    assert result.resolved_action == "proceed"


# --- 122: the probe forwards the Authorization header (auth-requiring upstream) ---


@pytest.mark.asyncio
async def test_probe_forwards_authorization_header():
    """check_health(headers=...) must send them — LiteLLM 401s a bare probe."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "c0", "type": "function",
                 "function": {"name": "ping", "arguments": "{}"}}]}}]},
        )

    target = _target()
    async with _client(handler) as client:
        result = await check_health(
            target, client=client, headers={"Authorization": "Bearer sk-test"}
        )
    assert seen["auth"] == "Bearer sk-test"
    assert result.status == "healthy"


@pytest.mark.asyncio
async def test_probe_without_headers_sends_none():
    """No headers (auth-free Ollama) → no Authorization sent (back-compat)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "<absent>")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "c0", "type": "function",
                 "function": {"name": "ping", "arguments": "{}"}}]}}]},
        )

    async with _client(handler) as client:
        await check_health(_target(), client=client)
    assert seen["auth"] == "<absent>"

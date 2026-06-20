"""099 (T004/T008/T010/T011): completion-style health probe mode.

A completion-mode target is gated on a non-empty NORMALIZED completion (no
tools), fail-closed on empty/broken — mirroring the tool-call probe's
normalize-then-judge order and gating. Default (tool_call) is unchanged.
"""
from __future__ import annotations

import json

import httpx
import pytest
import structlog

from performer.proxy.health import check_health
from performer.proxy.routing import TargetDescriptor


def _target(
    *,
    health_probe: str = "completion",
    strategy: str = "normalize",
    normalizers: list[str] | None = None,
    reroute_upstream: str | None = None,
    wire_format: str = "openai",
    base_url: str = "http://192.168.3.30:11434",
) -> TargetDescriptor:
    if normalizers is None:
        normalizers = ["strip_control_chars", "strip_reasoning"] if strategy == "normalize" else []
    return TargetDescriptor(
        base_url=base_url,
        wire_format=wire_format,
        strategy=strategy,
        normalizers=normalizers,
        reroute_upstream=reroute_upstream,
        health_probe=health_probe,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- T004 US1 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_probe_healthy_on_nonempty_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with _client(handler) as client:
        result = await check_health(_target(), model="gpt-oss:120b", client=client)
    assert result.status == "healthy"
    assert result.resolved_action == "proceed"


@pytest.mark.asyncio
async def test_completion_probe_empty_content_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

    async with _client(handler) as client:
        result = await check_health(_target(reroute_upstream=None), model="gpt-oss:120b", client=client)
    assert result.status == "unhealthy"
    assert result.resolved_action == "fail_closed"


@pytest.mark.asyncio
async def test_completion_probe_non200_unhealthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "overloaded"})

    async with _client(handler) as client:
        result = await check_health(_target(reroute_upstream=None), model="gpt-oss:120b", client=client)
    assert result.status == "unhealthy"


@pytest.mark.asyncio
async def test_completion_probe_timeout_unhealthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with _client(handler) as client:
        result = await check_health(_target(reroute_upstream=None), model="gpt-oss:120b", client=client, timeout=0.01)
    assert result.status == "unhealthy"


@pytest.mark.asyncio
async def test_completion_probe_normalize_then_judge_promotes_reasoning() -> None:
    """Empty content + populated reasoning_content; strip_reasoning promotes it →
    the NORMALIZED result is non-empty → healthy (FR-004)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "reasoning_content": "the answer"}}]
        })

    async with _client(handler) as client:
        result = await check_health(
            _target(normalizers=["strip_reasoning"]), model="gpt-oss:120b", client=client,
        )
    assert result.status == "healthy"


@pytest.mark.asyncio
async def test_completion_probe_body_carries_no_tools() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with _client(handler) as client:
        await check_health(_target(), model="gpt-oss:120b", client=client)
    assert "tools" not in captured["body"]
    assert "tool_choice" not in captured["body"]


# --- T010 US3: junie-style activation ---------------------------------------


@pytest.mark.asyncio
async def test_junie_style_completion_target_admitted() -> None:
    """A target shaped like the junie routing entry is admitted on a normal
    gpt-oss completion and fail-closed on a persistently empty body (FR-007)."""
    target = _target(
        strategy="normalize",
        normalizers=["strip_control_chars", "strip_reasoning"],
        base_url="http://192.168.3.30:11434",
    )

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "assessment text"}}]})

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    async with _client(ok) as client:
        assert (await check_health(target, model="gpt-oss:120b", client=client)).status == "healthy"
    async with _client(empty) as client:
        r = await check_health(target, model="gpt-oss:120b", client=client)
        assert r.status == "unhealthy" and r.resolved_action == "fail_closed"


# --- T011 US3: observability carries mode, secret-free ----------------------


@pytest.mark.asyncio
async def test_health_record_carries_probe_mode_secret_free() -> None:
    secret = "SECRET-tok-xyz the assessment is approved"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": secret}}]})

    with structlog.testing.capture_logs() as cap:
        async with _client(handler) as client:
            await check_health(_target(), model="gpt-oss:120b", client=client)
    rec = next(e for e in cap if e.get("event") == "proxy.health")
    assert rec["mode"] == "completion"
    assert secret not in repr(rec)

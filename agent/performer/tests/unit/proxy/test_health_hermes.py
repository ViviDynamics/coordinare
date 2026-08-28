"""100 US2: the hermes target composes with the 099 completion health probe.

tech_writer is a non-tool-calling JSON role, so its routed target uses the
completion probe — healthy on a non-empty normalized completion, fail-closed on
the empty/overloaded upstream the live probe observed. No new code: confirms the
099 probe is backend-agnostic and gates hermes correctly.
"""
from __future__ import annotations

import httpx
import pytest

from performer.proxy.health import check_health
from performer.proxy.routing import TargetDescriptor


def _hermes_target(reroute_upstream: str | None = None) -> TargetDescriptor:
    return TargetDescriptor(
        base_url="http://192.0.2.10:11434",
        wire_format="openai",
        strategy="normalize",
        normalizers=["strip_control_chars", "strip_reasoning"],
        health_probe="completion",
        reroute_upstream=reroute_upstream,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_hermes_completion_probe_healthy_on_nonempty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with _client(handler) as client:
        result = await check_health(_hermes_target(), model="gpt-oss:120b", client=client)
    assert result.status == "healthy" and result.resolved_action == "proceed"


@pytest.mark.asyncio
async def test_hermes_completion_probe_fail_closed_on_empty_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    async with _client(handler) as client:
        result = await check_health(_hermes_target(), model="gpt-oss:120b", client=client)
    assert result.status == "unhealthy" and result.resolved_action == "fail_closed"


@pytest.mark.asyncio
async def test_hermes_completion_probe_no_tools_in_body() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["b"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with _client(handler) as client:
        await check_health(_hermes_target(), model="gpt-oss:120b", client=client)
    assert "tools" not in captured["b"]

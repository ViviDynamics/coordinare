"""Opt-in Switchyard identity is stable within one job and isolated across jobs."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from performer.proxy.routing import TargetDescriptor
from performer.proxy.shim import SelfHostedShim


def target(**kwargs):
    return TargetDescriptor(base_url="http://sidecar:4000/v1", wire_format="openai",
                            strategy="translate", **kwargs)


def test_session_identity_stable_and_not_controlled_by_client():
    shim = SelfHostedShim(target(upstream_session_header="x-switchyard-session-id"))
    first = shim._forward_headers({"X-Switchyard-Session-ID": "client-global"})
    second = shim._forward_headers({})
    assert first == second
    assert first["x-switchyard-session-id"] != "client-global"
    other = SelfHostedShim(shim.target)
    assert other._forward_headers({}) != second


def test_default_does_not_add_session_header():
    assert SelfHostedShim(target())._forward_headers({"content-type": "application/json"}) == {
        "content-type": "application/json",
    }


def test_direct_reroute_cannot_silently_drop_identity():
    with pytest.raises(ValidationError, match="requires a shim"):
        TargetDescriptor(base_url="http://sidecar:4000", wire_format="openai",
                         strategy="reroute", upstream_session_header="x-switchyard-session-id")


def test_health_fallback_cannot_silently_drop_identity():
    with pytest.raises(ValidationError, match="health fallback"):
        target(upstream_session_header="x-switchyard-session-id", reroute_upstream="http://other:4000")


@pytest.mark.asyncio
async def test_dead_sidecar_fails_dispatch_health_gate():
    import httpx

    from performer.proxy.launch import ProxyLaunchError, _gate_target

    def unavailable(request):
        raise httpx.ConnectError("sidecar unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(ProxyLaunchError):
            await _gate_target(
                target(upstream_session_header="x-switchyard-session-id", health_probe="completion"),
                "claude_code", "agent", client=client, timeout=0.1, capture_dir=None,
            )


@pytest.mark.asyncio
async def test_reasoning_policy_cannot_bypass_session_header():
    from performer.proxy.launch import ProxyLaunchError, maybe_launch_proxy
    from performer.proxy.routing import RoutingTable

    routing = RoutingTable.model_validate({"entries": [{
        "backend": "claude_code", "model": "agent",
        "target": target(upstream_session_header="x-switchyard-session-id").model_dump(),
    }]})
    with pytest.raises(ProxyLaunchError, match="reasoning policies"):
        await maybe_launch_proxy(
            {"strategy": "single", "tool": {"model": "agent", "reasoning_policy": {"effort": "low"}}},
            "claude_code", routing_table=routing, model="agent", env={},
        )

"""100: hermes is routable through the self-hosted normalize shim.

hermes/tech_writer is JSON-only but was excluded from the layer
(UNSUPPORTED_BACKENDS). It now routes through the normalize shim with the 098
normalizer chain + 099 completion probe. Its CLI appends /chat/completions to
the provider base, so the loopback base needs a /v1 prefix (distinct from
junie's verbatim full-path suffix).
"""
from __future__ import annotations

import pytest

from performer.proxy.launch import (
    PROVIDER_BASE_URL_ENV,
    UNSUPPORTED_BACKENDS,
    VERBATIM_POST_WIRE_PATH,
    maybe_launch_proxy,
)
from performer.proxy.routing import RoutingEntry, RoutingTable, TargetDescriptor


def _hermes_table(model="gpt-oss:120b"):
    return RoutingTable(entries=[
        RoutingEntry(
            backend="hermes",
            model=model,
            target=TargetDescriptor(
                base_url="http://192.0.2.10:11434",
                wire_format="openai",
                strategy="normalize",
                health_probe="completion",
                normalizers=["strip_control_chars", "strip_reasoning"],
            ),
        )
    ])


def test_hermes_not_unsupported() -> None:
    assert "hermes" not in UNSUPPORTED_BACKENDS
    assert PROVIDER_BASE_URL_ENV.get("hermes") == "HERMES_BASE_URL"


@pytest.mark.asyncio
async def test_hermes_routes_through_normalize_shim_with_chain() -> None:
    env: dict[str, str] = {}
    shim = await maybe_launch_proxy(
        None, "hermes", env, routing_table=_hermes_table(), model="gpt-oss:120b",
    )
    try:
        assert shim is not None
        # eligible + provider env repointed at the loopback shim
        base = env["HERMES_BASE_URL"]
        assert base.startswith("http://127.0.0.1:")
        # 098 chain applied, same as junie
        assert [n.key for n in shim.normalizers] == ["strip_control_chars", "strip_reasoning"]
    finally:
        await shim.stop()
    assert "HERMES_BASE_URL" not in env


@pytest.mark.asyncio
async def test_hermes_loopback_base_has_v1_prefix() -> None:
    """hermes's CLI appends /chat/completions, so the loopback base must end in
    /v1 → the appended path lands on the served /v1/chat/completions route."""
    env: dict[str, str] = {}
    shim = await maybe_launch_proxy(
        None, "hermes", env, routing_table=_hermes_table(), model="gpt-oss:120b",
    )
    try:
        base = env["HERMES_BASE_URL"]
        assert base.endswith("/v1"), base
        assert not base.endswith("/v1/chat/completions"), "hermes is not verbatim-POST like junie"
    finally:
        await shim.stop()


def test_junie_verbatim_suffix_unchanged() -> None:
    # cross-contamination guard: junie keeps its full verbatim wire path
    assert VERBATIM_POST_WIRE_PATH.get("junie") == "/v1/chat/completions"
    assert "hermes" not in VERBATIM_POST_WIRE_PATH


@pytest.mark.asyncio
async def test_no_hermes_routing_entry_is_noop() -> None:
    """Default-safe: with no routing table, hermes is untouched (opt-in)."""
    env: dict[str, str] = {}
    assert await maybe_launch_proxy(None, "hermes", env) is None
    assert env == {}

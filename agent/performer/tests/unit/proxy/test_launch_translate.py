"""084 US3 — launch dispatch is unchanged for non-translate paths (T019).

TDD: written for the US3 launch surface. Covers FR-007 — adding a ``translate``
entry to the routing table must NOT perturb the existing ``reroute`` / ``normalize``
/ no-entry resolution paths (080/078 behavior). With a translate entry present:

* a ``reroute`` pair still repoints the provider env directly at the clean
  upstream and launches no shim runner;
* a ``normalize`` pair still launches a loopback shim carrying its normalizers;
* an unrouted ``(claude_code, <model>)`` pair is a byte-for-byte no-op (nothing
  launched, env untouched);
* the ``translate`` pair itself launches a shim in translate mode and points the
  Anthropic provider env at the loopback.
"""

from __future__ import annotations

import pytest

from performer.proxy.launch import maybe_launch_proxy
from performer.proxy.routing import RoutingEntry, RoutingTable, TargetDescriptor


def _mixed_table() -> RoutingTable:
    """A table that carries reroute, normalize, AND translate entries together."""
    return RoutingTable(
        entries=[
            RoutingEntry(
                backend="openclaw",
                model="reroute-model",
                target=TargetDescriptor(
                    base_url="http://ollama:11434",
                    wire_format="openai",
                    strategy="reroute",
                ),
            ),
            RoutingEntry(
                backend="openclaw",
                model="normalize-model",
                target=TargetDescriptor(
                    base_url="http://ollama:11434",
                    wire_format="openai",
                    strategy="normalize",
                    normalizers=["harmony_tool_calls"],
                ),
            ),
            RoutingEntry(
                backend="claude_code",
                model="translate-model",
                target=TargetDescriptor(
                    base_url="http://ollama:11434",
                    wire_format="openai",
                    strategy="translate",
                ),
            ),
        ]
    )


# --- FR-007: reroute path unchanged with a translate entry present ---------- #


@pytest.mark.asyncio
async def test_reroute_path_unchanged_with_translate_entry_present():
    env: dict[str, str] = {}
    shim = await maybe_launch_proxy(
        None, "openclaw", env, routing_table=_mixed_table(), model="reroute-model"
    )
    try:
        assert shim is not None
        assert env["OPENCLAW_PROVIDER_BASE_URL"] == "http://ollama:11434"
        # reroute launches no loopback runner and applies no normalizer
        assert shim._runner is None
        assert shim.normalizers == []
    finally:
        await shim.stop()
    assert "OPENCLAW_PROVIDER_BASE_URL" not in env


# --- FR-007: normalize path unchanged with a translate entry present -------- #


@pytest.mark.asyncio
async def test_normalize_path_unchanged_with_translate_entry_present():
    env: dict[str, str] = {}
    shim = await maybe_launch_proxy(
        None, "openclaw", env, routing_table=_mixed_table(), model="normalize-model"
    )
    try:
        assert shim is not None
        # normalize launches a loopback shim and points the env at it
        assert env["OPENCLAW_PROVIDER_BASE_URL"].startswith("http://127.0.0.1:")
        assert [n.key for n in shim.normalizers] == ["harmony_tool_calls"]
    finally:
        await shim.stop()
    assert "OPENCLAW_PROVIDER_BASE_URL" not in env


# --- FR-007: an unrouted claude_code pair is still a no-op ------------------ #


@pytest.mark.asyncio
async def test_unrouted_claude_code_pair_is_noop():
    env: dict[str, str] = {}
    # claude_code IS routed for translate-model, but a different model misses → no-op
    result = await maybe_launch_proxy(
        None, "claude_code", env, routing_table=_mixed_table(), model="some-other-model"
    )
    assert result is None
    assert env == {}


# --- the translate pair itself launches in translate mode ------------------- #


@pytest.mark.asyncio
async def test_translate_pair_launches_shim_and_points_anthropic_env():
    env: dict[str, str] = {}
    shim = await maybe_launch_proxy(
        None, "claude_code", env, routing_table=_mixed_table(), model="translate-model"
    )
    try:
        assert shim is not None
        # claude_code is pointed via ANTHROPIC_BASE_URL at the loopback shim
        assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
        assert shim.target.strategy == "translate"
    finally:
        await shim.stop()
    assert "ANTHROPIC_BASE_URL" not in env

"""080 — maybe_launch_proxy: backend env redirect + lifecycle (T025).

The aiohttp DualModelProxy actually starts (bound to 127.0.0.1:0); we assert the
right provider-base-URL env var is set to its loopback URL per backend, that
single/no-orchestration is a no-op, and that unsupported backends raise.
"""

from __future__ import annotations

import pytest

from performer.proxy.launch import (
    PROVIDER_BASE_URL_ENV,
    ProxyLaunchError,
    maybe_launch_proxy,
)

_ORCH = {
    "strategy": "always",
    "tool": {"name": "t", "model": "qwen", "wire_format": "openai", "base_url": "http://spark/v1"},
    "thinking": {"name": "th", "model": "gptoss", "wire_format": "openai", "base_url": "http://spark/v1"},
    "expose_plan_as": "thinking",
}


@pytest.mark.asyncio
async def test_no_orchestration_is_noop():
    env: dict[str, str] = {}
    assert await maybe_launch_proxy(None, "codex", env) is None
    assert env == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "env_var"),
    [
        ("codex", "CODEX_PROVIDER_BASE_URL"),
        ("opencode", "OPENCODE_PROVIDER_BASE_URL"),
        ("junie", "JUNIE_PROVIDER_BASE_URL"),
        ("pi", "PI_PROVIDER_BASE_URL"),
        ("openclaw", "OPENCLAW_PROVIDER_BASE_URL"),
        ("claude_code", "ANTHROPIC_BASE_URL"),
    ],
)
async def test_sets_provider_base_url_to_loopback(backend, env_var):
    env: dict[str, str] = {}
    proxy = await maybe_launch_proxy(_ORCH, backend, env)
    try:
        assert proxy is not None
        assert env[env_var].startswith("http://127.0.0.1:")
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_claude_code_suppresses_its_own_litellm_shim():
    env = {"LITELLM_PROXY_BASE_URL": "http://spark:4000"}
    proxy = await maybe_launch_proxy(_ORCH, "claude_code", env)
    try:
        # dual proxy takes over via ANTHROPIC_BASE_URL; claude's own shim var removed
        assert "LITELLM_PROXY_BASE_URL" not in env
        assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_kebab_case_backend_normalized():
    env: dict[str, str] = {}
    proxy = await maybe_launch_proxy(_ORCH, "claude-code", env)
    try:
        assert "ANTHROPIC_BASE_URL" in env
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_stop_restores_prior_env_so_no_cross_job_leak():
    """A long-lived process: after stop(), the provider env returns to its prior
    value (or is removed) so the next single-mode job doesn't inherit a dead URL."""
    env = {"CODEX_PROVIDER_BASE_URL": "http://prior:4000/v1"}
    proxy = await maybe_launch_proxy(_ORCH, "codex", env)
    assert env["CODEX_PROVIDER_BASE_URL"].startswith("http://127.0.0.1:")
    await proxy.stop()
    # restored to the prior value, not left pointing at the dead proxy
    assert env["CODEX_PROVIDER_BASE_URL"] == "http://prior:4000/v1"


@pytest.mark.asyncio
async def test_stop_removes_env_when_absent_before():
    env: dict[str, str] = {}
    proxy = await maybe_launch_proxy(_ORCH, "codex", env)
    assert "CODEX_PROVIDER_BASE_URL" in env
    await proxy.stop()
    assert "CODEX_PROVIDER_BASE_URL" not in env  # was absent → removed


@pytest.mark.asyncio
async def test_stop_restores_claude_litellm_shim_var():
    env = {"LITELLM_PROXY_BASE_URL": "http://spark:4000"}
    proxy = await maybe_launch_proxy(_ORCH, "claude_code", env)
    assert "LITELLM_PROXY_BASE_URL" not in env
    await proxy.stop()
    # both the suppressed shim var and the overridden base URL are restored
    assert env["LITELLM_PROXY_BASE_URL"] == "http://spark:4000"
    assert "ANTHROPIC_BASE_URL" not in env


@pytest.mark.asyncio
async def test_unsupported_backend_raises():
    with pytest.raises(ProxyLaunchError, match="no provider-base-URL override"):
        await maybe_launch_proxy(_ORCH, "hermes", {})


def test_all_supported_backends_have_env_mapping():
    # every backend coordinare can route a multi-model mode to must have a mapping
    for b in ("codex", "opencode", "junie", "pi", "openclaw", "claude_code"):
        assert b in PROVIDER_BASE_URL_ENV

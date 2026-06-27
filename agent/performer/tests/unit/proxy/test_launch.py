"""080 — maybe_launch_proxy: backend env redirect + lifecycle (T025).

The aiohttp DualModelProxy actually starts (bound to 127.0.0.1:0); we assert the
right provider-base-URL env var is set to its loopback URL per backend, that
single/no-orchestration is a no-op, and that unsupported backends raise.
"""

from __future__ import annotations

import pytest

from performer.proxy.launch import (
    PROVIDER_BASE_URL_ENV,
    VERBATIM_POST_WIRE_PATH,
    ProxyLaunchError,
    maybe_launch_proxy,
)
from performer.proxy.routing import RoutingEntry, RoutingTable, TargetDescriptor

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


# --- 078: routing-table no-op (SC-003) -------------------------------------- #


def _reroute_table(backend="openclaw", model="gpt-oss:120b", base_url="http://ollama:11434"):
    return RoutingTable(
        entries=[
            RoutingEntry(
                backend=backend,
                model=model,
                target=TargetDescriptor(
                    base_url=base_url, wire_format="openai", strategy="reroute"
                ),
            )
        ]
    )


@pytest.mark.asyncio
async def test_no_routing_table_and_no_orchestration_is_noop():
    """Neither a routing table nor orchestration → byte-for-byte no-op."""
    env: dict[str, str] = {}
    assert await maybe_launch_proxy(None, "codex", env) is None
    assert env == {}


@pytest.mark.asyncio
async def test_routing_miss_is_noop():
    """A routing table that does not resolve (backend, model) is a no-op (SC-003):
    nothing launched, no provider-env override set."""
    env: dict[str, str] = {}
    table = _reroute_table()
    # different backend → miss
    assert await maybe_launch_proxy(None, "codex", env, routing_table=table, model="gpt-oss:120b") is None
    assert env == {}
    # different model → miss
    assert await maybe_launch_proxy(None, "openclaw", env, routing_table=table, model="other") is None
    assert env == {}


@pytest.mark.asyncio
async def test_routing_without_model_is_noop():
    """A routing table but no model to resolve against cannot match → no-op."""
    env: dict[str, str] = {}
    table = _reroute_table()
    assert await maybe_launch_proxy(None, "openclaw", env, routing_table=table) is None
    assert env == {}


@pytest.mark.asyncio
async def test_routing_hit_reroute_sets_provider_env_directly():
    """A reroute hit repoints the provider env straight at the clean upstream and
    launches no shim runner (its env restore lifecycle is still uniform)."""
    env: dict[str, str] = {}
    table = _reroute_table(base_url="http://ollama:11434")
    shim = await maybe_launch_proxy(None, "openclaw", env, routing_table=table, model="gpt-oss:120b")
    try:
        assert shim is not None
        assert env["OPENCLAW_PROVIDER_BASE_URL"] == "http://ollama:11434"
    finally:
        await shim.stop()
    # stop() restores env → no cross-job leak
    assert "OPENCLAW_PROVIDER_BASE_URL" not in env


@pytest.mark.asyncio
async def test_reroute_launches_no_shim_and_no_normalizer(monkeypatch):
    """T019/SC-004/FR-078-4: a reroute hit repoints the correct
    PROVIDER_BASE_URL_ENV var at the clean upstream, launches NO shim runner,
    applies NO normalizer, and restores the env after the job."""
    monkeypatch.setenv("OPENCLAW_PROVIDER_BASE_URL", "http://litellm:4000")
    env = {"OPENCLAW_PROVIDER_BASE_URL": "http://litellm:4000"}
    table = _reroute_table(base_url="http://ollama:11434")
    shim = await maybe_launch_proxy(
        None, "openclaw", env, routing_table=table, model="gpt-oss:120b"
    )
    try:
        assert shim is not None
        # correct provider env var repointed straight at the clean upstream
        assert env["OPENCLAW_PROVIDER_BASE_URL"] == "http://ollama:11434"
        # no loopback shim runner started — reroute skips the middleware entirely
        assert shim._runner is None
        # no normalizer applied: reroute is a direct repoint, not a translation
        assert shim.normalizers == []
        # a body flows through byte-for-byte even though a normalizer registry exists
        body = {"choices": [{"message": {"content": "untouched"}}]}
        assert shim.normalize_json(body) == body
    finally:
        await shim.stop()
    # env restored to its prior value after the job → no cross-job leak
    assert env["OPENCLAW_PROVIDER_BASE_URL"] == "http://litellm:4000"


def _junie_normalize_table(model="gpt-oss:120b", base_url="http://192.168.3.30:11434"):
    return RoutingTable(
        entries=[
            RoutingEntry(
                backend="junie",
                model=model,
                target=TargetDescriptor(
                    base_url=base_url,
                    wire_format="openai",
                    strategy="normalize",
                    normalizers=["strip_control_chars", "strip_reasoning"],
                ),
            )
        ]
    )


@pytest.mark.asyncio
async def test_junie_normalize_shim_carries_wire_path_and_normalizers():
    """098 US2: routing junie through a normalize-mode SelfHostedShim must (a)
    apply the declared normalizers (control-char strip + reasoning promote) and
    (b) hand junie the FULL wire-path URL — junie POSTs verbatim and the shim
    only serves pathed front doors, so a bare loopback root would 404 into the
    exact 'Failed to build issue.md' failure this feature fixes."""
    env: dict[str, str] = {}
    table = _junie_normalize_table()
    shim = await maybe_launch_proxy(
        None, "junie", env, routing_table=table, model="gpt-oss:120b"
    )
    try:
        assert shim is not None
        base = env["JUNIE_PROVIDER_BASE_URL"]
        assert base.startswith("http://127.0.0.1:")
        assert base.endswith(VERBATIM_POST_WIRE_PATH["junie"]), base
        assert [n.key for n in shim.normalizers] == [
            "strip_control_chars",
            "strip_reasoning",
        ]
    finally:
        await shim.stop()
    assert "JUNIE_PROVIDER_BASE_URL" not in env


@pytest.mark.asyncio
async def test_routing_entry_for_unmappable_backend_raises():
    """T020/FR-078-4 Edge Case: a backend with no PROVIDER_BASE_URL_ENV mapping
    appearing in a ROUTING ENTRY surfaces a clear cannot-route error rather than
    silently no-opping into a broken path. (hermes is now mapped — spec 100 — so
    use a genuinely-unmapped backend name here.)"""
    table = _reroute_table(backend="ghostbackend", model="some-model")
    with pytest.raises(ProxyLaunchError, match="no provider-base-URL override"):
        await maybe_launch_proxy(
            None, "ghostbackend", {}, routing_table=table, model="some-model"
        )


@pytest.mark.asyncio
async def test_routing_takes_precedence_over_orchestration():
    """An explicit routing entry pins the path regardless of an orchestration block."""
    env: dict[str, str] = {}
    table = _reroute_table(base_url="http://ollama:11434")
    shim = await maybe_launch_proxy(_ORCH, "openclaw", env, routing_table=table, model="gpt-oss:120b")
    try:
        # routed straight at the clean upstream, NOT a 127.0.0.1 dual-model proxy
        assert env["OPENCLAW_PROVIDER_BASE_URL"] == "http://ollama:11434"
    finally:
        await shim.stop()


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
async def test_junie_provider_base_url_carries_full_wire_path():
    """082r10 regression: junie POSTs verbatim to JUNIE_PROVIDER_BASE_URL (junie.py
    requires the FULL endpoint URL, not a /v1 base — it appends no path of its own).
    The DualModelProxy serves pathed front doors (/v1/chat/completions, …) but NOT a
    bare root, so handing junie the bare proxy root makes its standalone build POST to
    `/` → miss every served path → "Failed to build 'issue.md.junie_standalone'" before
    any inference. The proxy base for junie must therefore carry the openai wire path."""
    env: dict[str, str] = {}
    proxy = await maybe_launch_proxy(_ORCH, "junie", env)
    try:
        assert proxy is not None
        base = env["JUNIE_PROVIDER_BASE_URL"]
        assert base.startswith("http://127.0.0.1:")
        # full verbatim-POST endpoint, NOT the bare proxy root
        assert base.endswith("/v1/chat/completions"), base
    finally:
        await proxy.stop()
    # stop() still restores cleanly even with the suffix applied
    assert "JUNIE_PROVIDER_BASE_URL" not in env


def test_verbatim_post_backends_use_a_served_proxy_path():
    """Every verbatim-POST suffix must be a path the DualModelProxy actually serves,
    else the appended URL would 404 just like the bare root did."""
    from performer.proxy.dual_model_proxy import _PATH_WIRE

    for backend, suffix in VERBATIM_POST_WIRE_PATH.items():
        assert backend in PROVIDER_BASE_URL_ENV, backend
        assert suffix in _PATH_WIRE, (backend, suffix)


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
    # A backend with no PROVIDER_BASE_URL_ENV mapping can't run a multi-model mode.
    # (hermes is now mapped — spec 100 — so this uses a genuinely-unmapped name.)
    with pytest.raises(ProxyLaunchError, match="not supported by the dual-model proxy"):
        await maybe_launch_proxy(_ORCH, "ghostbackend", {})


def test_all_supported_backends_have_env_mapping():
    # every backend coordinare can route a multi-model mode to must have a mapping
    for b in ("codex", "opencode", "junie", "pi", "openclaw", "claude_code", "hermes"):
        assert b in PROVIDER_BASE_URL_ENV


@pytest.mark.asyncio
async def test_health_timeout_threads_through_to_probe(monkeypatch):
    """The caller-supplied ``health_timeout`` must reach ``check_health`` so a
    big self-hosted model (gpt-oss:120b) gets a cold-load-tolerant probe budget
    rather than the 10s default that times out during VRAM load (FR-078-5)."""
    from performer.proxy.health import HealthResult

    seen: dict = {}

    async def fake_check_health(target, *, model=None, client=None, timeout=10.0, headers=None):
        seen["timeout"] = timeout
        seen["model"] = model
        return HealthResult(
            target=target, status="healthy", reason=None, resolved_action="proceed"
        )

    monkeypatch.setattr("performer.proxy.launch.check_health", fake_check_health)
    env: dict[str, str] = {}
    table = _reroute_table(base_url="http://ollama:11434")
    shim = await maybe_launch_proxy(
        None,
        "openclaw",
        env,
        routing_table=table,
        model="gpt-oss:120b",
        health_check=True,
        health_timeout=120.0,
    )
    try:
        assert seen["timeout"] == 120.0
        assert seen["model"] == "gpt-oss:120b"
    finally:
        await shim.stop()

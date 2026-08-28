"""082 US9 / FR-011 — codex wire_api cleared so the dual-model proxy serves it.

The benchmark/prod config sets ``CODEX_PROVIDER_WIRE_API=responses`` for direct
LiteLLM runs, where codex calls the OpenAI Responses API (``POST /responses``).
But the in-container ``DualModelProxy`` is a normalizing reverse proxy that serves
ONLY chat-completions/messages — it has no ``/responses`` route. So when dual-model
is active for codex, the launcher must steer codex onto chat-completions.

codex 0.137.0 rejects every explicit chat-flavoured ``wire_api`` string ("chat",
"chat_completions", "completions", …) as an invalid enum variant: it swallows the
config error, falls back to a default config that lacks the custom provider, and
dies with the misleading ``Model provider `vivi` not found``. The ONLY values it
accepts are ``responses`` (explicit) or *omitting* ``wire_api`` entirely — in which
case codex defaults to chat-completions, exactly the path the proxy serves.

So the launcher REMOVES ``CODEX_PROVIDER_WIRE_API`` (restorably) under the proxy,
which makes codex.py omit the ``wire_api`` line from config.toml and codex fall back
to its chat-completions default. Direct (non-proxy) codex runs are untouched: the
override only fires inside ``maybe_launch_proxy`` and is reverted on ``stop()``.
"""
from __future__ import annotations

from performer.proxy.launch import maybe_launch_proxy


def _orchestration() -> dict:
    return {
        "strategy": "always",
        "thinking": {"name": "planner", "model": "p", "wire_format": "openai", "base_url": "http://p.local"},
        "tool": {"name": "exec", "model": "e", "wire_format": "openai", "base_url": "http://e.local"},
    }


async def test_codex_wire_api_cleared_under_proxy() -> None:
    env = {
        "CODEX_PROVIDER_WIRE_API": "responses",
        "CODEX_PROVIDER_BASE_URL": "https://litellm.example/v1",
    }
    proxy = await maybe_launch_proxy(_orchestration(), "codex", env)
    try:
        assert proxy is not None
        # base URL repointed at the loopback proxy ...
        assert env["CODEX_PROVIDER_BASE_URL"].startswith("http://127.0.0.1")
        # ... and wire_api REMOVED (any explicit chat string is an invalid enum in
        # codex 0.137.0; omitting it makes codex default to chat-completions, which
        # the proxy serves — no /responses route, no config-error fallback).
        assert "CODEX_PROVIDER_WIRE_API" not in env
    finally:
        await proxy.stop()
    # stop() restores the operator's original value for the next (direct) job.
    assert env["CODEX_PROVIDER_WIRE_API"] == "responses"


async def test_non_codex_backend_gets_no_wire_api_pin() -> None:
    """Only codex carries CODEX_PROVIDER_WIRE_API; other backends must not gain it."""
    env = {"OPENCODE_PROVIDER_BASE_URL": "https://litellm.example/v1"}
    proxy = await maybe_launch_proxy(_orchestration(), "opencode", env)
    try:
        assert "CODEX_PROVIDER_WIRE_API" not in env
    finally:
        await proxy.stop()

"""080 — launch the DualModelProxy and redirect a backend at it.

Backend-agnostic glue invoked at job start: when the dispatch carries an
``orchestration`` block (strategy != single), start an in-container
``DualModelProxy`` and set the backend's provider-base-URL env var to the
proxy's loopback URL, so the agent CLI's model calls flow through the proxy.

When there is no orchestration block (single mode), this is a complete no-op —
the proxy is never started and no env is touched.

The proxy speaks both wire formats on its front door (``/v1/messages`` and
``/v1/chat/completions``), so the same proxy serves any backend; only the env
var the backend reads differs.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from performer.proxy.dual_model_proxy import DualModelProxy

log = structlog.get_logger(__name__)

# Per-backend env var the CLI reads as its model-provider base URL.
PROVIDER_BASE_URL_ENV: dict[str, str] = {
    "codex": "CODEX_PROVIDER_BASE_URL",
    "opencode": "OPENCODE_PROVIDER_BASE_URL",
    "opencode_compat": "OPENCODE_PROVIDER_BASE_URL",
    "junie": "JUNIE_PROVIDER_BASE_URL",
    "pi": "PI_PROVIDER_BASE_URL",
    "openclaw": "OPENCLAW_PROVIDER_BASE_URL",
    # claude_code is pointed via ANTHROPIC_BASE_URL; its own LiteLLM shim is
    # suppressed (see below) so it does not double-proxy.
    "claude_code": "ANTHROPIC_BASE_URL",
}

# Backends with no provider-base-URL override cannot be proxied (080 constraint).
UNSUPPORTED_BACKENDS = frozenset({"hermes"})


class ProxyLaunchError(RuntimeError):
    """Raised when a backend cannot be routed through the dual-model proxy."""


async def maybe_launch_proxy(
    orchestration: dict[str, Any] | None,
    backend_name: str,
    env: dict[str, str] | None = None,
) -> DualModelProxy | None:
    """Start the proxy and redirect ``backend_name`` at it, if orchestration set.

    ``env`` is the mutable environment mapping to update (defaults to
    ``os.environ``). Returns the started ``DualModelProxy`` (caller must
    ``stop()`` it on job teardown) or ``None`` when there is nothing to do.
    """
    if not orchestration:
        return None  # single mode / no orchestration → no proxy
    backend = backend_name.replace("-", "_").lower()
    if backend in UNSUPPORTED_BACKENDS:
        raise ProxyLaunchError(
            f"backend '{backend}' has no provider-base-URL override; it cannot run a "
            "multi-model mode (080) — use strategy: single for it"
        )
    env_var = PROVIDER_BASE_URL_ENV.get(backend)
    if env_var is None:
        raise ProxyLaunchError(f"backend '{backend}' is not supported by the dual-model proxy")

    target = os.environ if env is None else env
    proxy = DualModelProxy(
        orchestration=orchestration,
        expose_plan_as=orchestration.get("expose_plan_as", "thinking"),
    )
    base = await proxy.start()
    # Capture prior values BEFORE mutating so proxy.stop() can restore them —
    # otherwise a long-lived performer process leaks this (now-dead) proxy URL
    # into a later single-mode job for the same backend (review finding).
    proxy.env_restores.append((target, env_var, target.get(env_var)))
    target[env_var] = base
    # claude_code would otherwise launch its own LiteLLM shim from this var and
    # double-proxy; suppress it so it talks to the dual-model proxy directly.
    if backend == "claude_code":
        proxy.env_restores.append((target, "LITELLM_PROXY_BASE_URL", target.get("LITELLM_PROXY_BASE_URL")))
        target.pop("LITELLM_PROXY_BASE_URL", None)
    log.info(
        "dual_model_proxy.launched",
        backend=backend, env_var=env_var, strategy=orchestration.get("strategy"),
    )
    return proxy

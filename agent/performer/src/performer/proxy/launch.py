"""080 + 078 — launch a proxy/shim and redirect a backend at it.

Backend-agnostic glue invoked at job start. Two activation surfaces share this
seam:

* **080 (orchestration)** — when the dispatch carries an ``orchestration`` block
  (strategy != single), start an in-container ``DualModelProxy`` and set the
  backend's provider-base-URL env var to the proxy's loopback URL.
* **078 (self-hosted robustness layer)** — when a routing table resolves the
  ``(backend, model)`` pair to a :class:`~performer.proxy.routing.TargetDescriptor`,
  dispatch on ``target.strategy``:

  - ``normalize`` — launch a loopback :class:`~performer.proxy.shim.SelfHostedShim`
    (applying the target's declared normalizers) and point the provider env at it.
  - ``reroute`` — repoint the provider env directly at ``target.base_url`` (a clean
    upstream) and launch no shim at all.

A ``(backend, model)`` pair with **no routing entry** AND no orchestration block
is a byte-for-byte no-op: nothing is launched and no env is touched (FR-078-1/4).

Both ``DualModelProxy`` and ``SelfHostedShim`` expose ``stop()`` (which also
restores any env this seam mutated), so the caller tears either down uniformly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from performer.proxy.dual_model_proxy import DualModelProxy
from performer.proxy.health import check_health
from performer.proxy.normalizers import NORMALIZER_REGISTRY
from performer.proxy.routing import TargetDescriptor, _normalize_backend
from performer.proxy.shim import SelfHostedShim

if TYPE_CHECKING:
    import httpx

    from performer.proxy.routing import RoutingTable

log = structlog.get_logger(__name__)

# Per-backend env var the CLI reads as its model-provider base URL.
PROVIDER_BASE_URL_ENV: dict[str, str] = {
    "codex": "CODEX_PROVIDER_BASE_URL",
    "opencode": "OPENCODE_PROVIDER_BASE_URL",
    "opencode_compat": "OPENCODE_PROVIDER_BASE_URL",
    "junie": "JUNIE_PROVIDER_BASE_URL",
    "pi": "PI_PROVIDER_BASE_URL",
    "prime_agent": "PRIME_AGENT_PROVIDER_BASE_URL",
    "openclaw": "OPENCLAW_PROVIDER_BASE_URL",
    "hermes": "HERMES_BASE_URL",
    # claude_code is pointed via ANTHROPIC_BASE_URL; its own LiteLLM shim is
    # suppressed (see below) so it does not double-proxy.
    "claude_code": "ANTHROPIC_BASE_URL",
}

# Backends with no provider-base-URL override cannot be proxied (080 constraint).
UNSUPPORTED_BACKENDS: frozenset[str] = frozenset()

# Backends that POST *verbatim* to their provider-base-URL env var — i.e. the CLI
# appends NO path of its own (junie.py requires the FULL endpoint URL and POSTs it
# as-is). For these, the bare DualModelProxy root (``http://host:port``) is wrong:
# the proxy serves only pathed front doors (see ``_PATH_WIRE``), so a verbatim POST
# to ``/`` misses every served path and the CLI's standalone build fails before any
# inference (082r10 regression). Append the backend's wire-format endpoint path so the
# env var carries a complete, served URL. CLIs that append their own path (codex,
# opencode, openclaw, pi, claude_code) are absent here and keep the bare root.
VERBATIM_POST_WIRE_PATH: dict[str, str] = {
    "junie": "/v1/chat/completions",  # junie speaks the OpenAI chat wire format
}

# Backends whose CLI DOES append its own ``/chat/completions`` to the provider
# base, but expects that base to carry the ``/v1`` prefix (OpenAI-compat). For
# these the loopback root is wrong (the shim serves ``/v1/chat/completions``), so
# we hand them ``<loopback>/v1`` and their appended path lands on the served route.
# This is the COMPLEMENT of VERBATIM_POST_WIRE_PATH: junie posts the full path
# verbatim; these post their own suffix onto a ``/v1`` base. A backend belongs to
# at most one map (100: hermes uses the hermes-agent custom OpenAI provider).
PROVIDER_BASE_PATH_PREFIX: dict[str, str] = {
    "hermes": "/v1",
}


class ProxyLaunchError(RuntimeError):
    """Raised when a backend cannot be routed through the dual-model proxy."""


def _with_verbatim_wire_path(backend: str, base: str) -> str:
    """Append a backend's required path onto a loopback base URL.

    Two disjoint cases, both so the CLI's real request lands on a route the
    loopback shim/proxy actually serves (it serves only pathed front doors):

    * **verbatim-POST** backends (junie) append NO path of their own, so we hand
      them the FULL served wire path (``VERBATIM_POST_WIRE_PATH``).
    * **base-prefix** backends (hermes, OpenAI-compat custom provider) DO append
      ``/chat/completions``, so we hand them just the ``/v1`` prefix
      (``PROVIDER_BASE_PATH_PREFIX``) and their suffix completes the served path.

    CLIs that build their own path off a bare root (codex, opencode, openclaw, pi,
    claude_code) are in neither map and get the bare root unchanged. Shared by the
    dual-model proxy (080) and the self-hosted shim (078/098/100) seams.
    """
    return (
        base
        + VERBATIM_POST_WIRE_PATH.get(backend, "")
        + PROVIDER_BASE_PATH_PREFIX.get(backend, "")
    )


def _emit_health_decision(
    capture_dir: str | Path | None, backend: str, model: str | None, result,
) -> None:
    """Append the gating decision to ``capture_dir/health.jsonl`` (FR-078-10).

    Only the decision summary is written — never tokens or request/response
    bodies. Best-effort: an unwritable capture dir must not block startup.
    """
    if capture_dir is None:
        return
    record = {
        "event": "proxy.health",
        "backend": backend,
        "model": model,
        "base_url": result.target.base_url,
        "wire_format": result.target.wire_format,
        "strategy": result.target.strategy,
        # 100: record the health-probe mode (099) so completion- vs tool-call-probed
        # targets are distinguishable in the capture record (contract FR-006).
        "mode": getattr(result.target, "health_probe", "tool_call"),
        "status": result.status,
        "resolved_action": result.resolved_action,
        "reason": result.reason,
    }
    try:
        path = Path(capture_dir)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "health.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:  # pragma: no cover - defensive, never blocks startup
        log.warning("selfhosted_layer.health_capture_failed", error=repr(exc))


def _suppress_double_proxy(
    backend: str, mapping: dict[str, str], env_restores: list,
) -> None:
    """claude_code would relaunch its own LiteLLM shim from ``LITELLM_PROXY_BASE_URL``
    and double-proxy; suppress it (restorably) so it talks to our seam directly.
    """
    if backend == "claude_code":
        env_restores.append(
            (mapping, "LITELLM_PROXY_BASE_URL", mapping.get("LITELLM_PROXY_BASE_URL")),
        )
        mapping.pop("LITELLM_PROXY_BASE_URL", None)


async def _launch_for_target(
    target: "TargetDescriptor",
    backend_name: str,
    env: dict[str, str] | None,
) -> SelfHostedShim:
    """Dispatch the 078 self-hosted layer for a resolved routing target.

    ``normalize`` launches a loopback shim and points the provider env at it;
    ``reroute`` repoints the provider env straight at ``target.base_url`` and
    launches no shim (a shim is not always the answer). Both return a
    ``SelfHostedShim`` so the caller's ``stop()`` teardown — and env restore —
    is uniform; the reroute shim simply never starts a runner.
    """
    backend = _normalize_backend(backend_name)
    env_var = PROVIDER_BASE_URL_ENV.get(backend)
    if env_var is None:
        raise ProxyLaunchError(
            f"backend '{backend}' has no provider-base-URL override; it cannot be "
            f"routed at self-hosted target {target.base_url!r} (FR-078-4) — "
            "no silent no-op into a broken path",
        )
    mapping = os.environ if env is None else env

    if target.strategy == "reroute":
        # Repoint the provider env directly at the clean upstream; no shim.
        shim = SelfHostedShim(target=target)
        shim.env_restores.append((mapping, env_var, mapping.get(env_var)))
        mapping[env_var] = target.base_url
        _suppress_double_proxy(backend, mapping, shim.env_restores)
        log.info(
            "selfhosted_layer.rerouted",
            backend=backend, env_var=env_var, base_url=target.base_url,
        )
        return shim

    if target.strategy == "translate":
        # Launch the loopback shim in translate mode (the shim reads
        # target.strategy) and point the Anthropic provider env at it, so the
        # claude_code CLI's /v1/messages traffic is translated to the OpenAI-wire
        # upstream with no LiteLLM in the path (spec 084, FR-006). Normalizers are
        # OPTIONAL for translate and compose beneath the wire translation
        # (Decision 4); an empty list is a pure-translation shim.
        normalizers = [NORMALIZER_REGISTRY[k] for k in target.normalizers]
        shim = SelfHostedShim(target=target, normalizers=normalizers)
        base = await shim.start()
        shim.env_restores.append((mapping, env_var, mapping.get(env_var)))
        mapping[env_var] = _with_verbatim_wire_path(backend, base)
        _suppress_double_proxy(backend, mapping, shim.env_restores)
        log.info(
            "selfhosted_layer.translate_launched",
            backend=backend,
            env_var=env_var,
            normalizers=[n.key for n in normalizers],
        )
        return shim

    # strategy == "normalize" (apply declared normalizers) OR "observe" (122
    # Decision 6: verbatim passthrough — normalizers is empty, so the shim only
    # forwards the body + logs latency/status + writes capture_dir, no transform).
    normalizers = [NORMALIZER_REGISTRY[k] for k in target.normalizers]
    shim = SelfHostedShim(target=target, normalizers=normalizers)
    base = await shim.start()
    shim.env_restores.append((mapping, env_var, mapping.get(env_var)))
    mapping[env_var] = _with_verbatim_wire_path(backend, base)
    _suppress_double_proxy(backend, mapping, shim.env_restores)
    log.info(
        f"selfhosted_layer.{target.strategy}_launched",
        backend=backend, env_var=env_var, normalizers=[n.key for n in normalizers],
    )
    return shim


async def _gate_target(
    target: "TargetDescriptor",
    backend_name: str,
    model: str | None,
    *,
    client: "httpx.AsyncClient | None",
    timeout: float,
    capture_dir: str | Path | None,
) -> "TargetDescriptor":
    """Smoke-test ``target`` and resolve the FR-078-5 gate before launch.

    * ``healthy`` / ``proceed`` → return ``target`` unchanged.
    * ``unhealthy`` + ``reroute_upstream`` → auto-reroute: return a clean
      ``reroute`` target pointed at ``reroute_upstream`` (the Ollama-direct fix).
    * ``unhealthy`` + none → ``fail_closed``: raise ``ProxyLaunchError`` with the
      probe's reason so the card is not accepted against a broken path.

    The decision is emitted to ``capture_dir`` (decision summary only, never
    tokens/bodies, FR-078-10).
    """
    # 122: an auth-requiring upstream (e.g. LiteLLM) 401s a bare probe — every
    # prior target was auth-free Ollama. Send the same provider credential the
    # real CLI traffic uses (Bearer), pulled from the provider-auth env. Auth-free
    # upstreams (Ollama) ignore the header, so this is safe for all targets.
    # 123: prefer the target's ``upstream_auth_env`` FIRST so the probe authenticates
    # with the exact key the shim injects into traffic (``_forward_headers``). This
    # matters when the generic OPENAI_API_KEY is poisoned — the coordinare injects the
    # daemon's REAL OpenAI key into opencode/junie containers, which would 401 the
    # probe; the dedicated upstream_auth_env carries the proxy bearer instead.
    _probe_tok = ""
    _auth_env = getattr(target, "upstream_auth_env", None)
    if _auth_env:
        _probe_tok = (os.environ.get(_auth_env) or "").strip()
    if not _probe_tok:
        _probe_tok = (
            os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("LITELLM_PROXY_AUTH_TOKEN")
            or ""
        ).strip()
    _probe_headers = {"Authorization": f"Bearer {_probe_tok}"} if _probe_tok else None
    result = await check_health(
        target, model=model, client=client, timeout=timeout, headers=_probe_headers,
    )
    _emit_health_decision(capture_dir, backend_name, model, result)

    if result.resolved_action == "proceed":
        return target
    if result.resolved_action == "rerouted":
        # The reroute target is intentionally TERMINAL: it carries no
        # reroute_upstream of its own, so it is not re-probed or re-gated. The
        # auto-reroute is a single hop to a known-clean upstream (the
        # Ollama-direct fix); if that upstream is itself down the forwarded
        # request fails at request time rather than chaining a second fallback.
        return TargetDescriptor(
            base_url=target.reroute_upstream,
            wire_format=target.wire_format,
            strategy="reroute",
        )
    # fail_closed
    raise ProxyLaunchError(
        f"self-hosted path for backend '{backend_name}' model '{model}' failed its "
        f"startup health probe and has no reroute_upstream: {result.reason} "
        "(FR-078-5) — card not accepted rather than black-holed",
    )


async def maybe_launch_proxy(
    orchestration: dict[str, Any] | None,
    backend_name: str,
    env: dict[str, str] | None = None,
    *,
    routing_table: "RoutingTable | None" = None,
    model: str | None = None,
    health_check: bool = False,
    health_client: "httpx.AsyncClient | None" = None,
    health_timeout: float = 10.0,
    capture_dir: str | Path | None = None,
) -> DualModelProxy | SelfHostedShim | None:
    """Start a proxy/shim and redirect ``backend_name`` at it, if activated.

    Resolution order:

    1. **078 routing** — if ``routing_table`` resolves ``(backend_name, model)``
       to a target, dispatch on its strategy and return a ``SelfHostedShim``.
    2. **080 orchestration** — else if ``orchestration`` is set, start the
       ``DualModelProxy``.
    3. **No-op** — else return ``None`` (byte-for-byte; no launch, no env touch).

    ``env`` is the mutable environment mapping to update (defaults to
    ``os.environ``). The returned object (if any) exposes ``stop()``, which the
    caller MUST invoke on job teardown to restore env.
    """
    # 163: an opted-in policy must reach each model independently, including
    # Responses clients. Canonical orchestration accepts all three CLI wires.
    policy_active = bool(orchestration and any(
        isinstance(orchestration.get(leg), dict) and orchestration[leg].get("reasoning_policy")
        for leg in ("tool", "thinking", "classifier")
    ))
    if policy_active:
        orchestration = {
            key: {**value, "preserve_generation": True} if key in {"tool", "thinking", "classifier"}
            and isinstance(value, dict) else value
            for key, value in orchestration.items()
        }
    if policy_active and routing_table is not None:
        # Apply existing response repairs/auth/reroute to each matching leg;
        # policy is never borrowed from the executor for a different model.
        orchestration = dict(orchestration)
        for leg in ("tool", "thinking", "classifier"):
            ref = orchestration.get(leg)
            if not isinstance(ref, dict):
                continue
            target = routing_table.resolve(backend_name, ref["model"])
            if target is None:
                continue
            if target.upstream_session_header is not None:
                raise ProxyLaunchError(
                    "upstream_session_header requires the self-hosted shim; "
                    "reasoning policies use the canonical proxy and are not supported",
                )
            if health_check:
                target = await _gate_target(target, backend_name, ref["model"], client=health_client,
                                            timeout=health_timeout, capture_dir=capture_dir)
            if target.wire_format != "openai":
                raise ProxyLaunchError("reasoning policy routing requires an OpenAI-compatible upstream")
            orchestration[leg] = {**ref, "base_url": target.base_url,
                                  "wire_format": target.wire_format, "auth_style": "bearer",
                                  "model": target.upstream_model or ref["model"],
                                  "normalizers": target.normalizers,
                                  "auth_env": target.upstream_auth_env or ref.get("auth_env")}

    # 078 takes precedence: an explicit routing entry pins how this backend
    # reaches its self-hosted upstream regardless of orchestration mode.
    if not policy_active and routing_table is not None and model is not None:
        target = routing_table.resolve(backend_name, model)
        if target is not None:
            if health_check:
                target = await _gate_target(
                    target,
                    backend_name,
                    model,
                    client=health_client,
                    timeout=health_timeout,
                    capture_dir=capture_dir,
                )
            return await _launch_for_target(target, backend_name, env)

    if not orchestration:
        return None  # no routing entry + single mode / no orchestration → no-op
    backend = backend_name.replace("-", "_").lower()
    # UNSUPPORTED_BACKENDS is currently empty (100 removed hermes); this data-driven
    # guard stays so re-adding a backend to the set restores the clear error. The
    # env-mapping check below is the live safety net for any unmapped backend.
    if backend in UNSUPPORTED_BACKENDS:
        raise ProxyLaunchError(
            f"backend '{backend}' has no provider-base-URL override; it cannot run a "
            "multi-model mode (080) — use strategy: single for it",
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
    # Verbatim-POST backends (junie) need the full served wire path appended; CLIs
    # that build their own path off the base get the bare proxy root unchanged.
    target[env_var] = _with_verbatim_wire_path(backend, base)
    # claude_code would otherwise launch its own LiteLLM shim from this var and
    # double-proxy; suppress it so it talks to the dual-model proxy directly.
    if backend == "claude_code":
        proxy.env_restores.append((target, "LITELLM_PROXY_BASE_URL", target.get("LITELLM_PROXY_BASE_URL")))
        target.pop("LITELLM_PROXY_BASE_URL", None)
    # codex is configured with wire_api=responses for direct LiteLLM runs. codex
    # 0.137.0 rejects every explicit chat-flavoured wire_api string ("chat",
    # "chat_completions", "completions", …) as an invalid enum variant — it swallows
    # the config error, falls back to a default config lacking the custom provider,
    # and dies with a misleading "Model provider not found". Only "responses" or
    # OMITTING wire_api lets the app-server START. So REMOVE the var (restorably):
    # codex.py omits the wire_api line and the app-server boots.
    #
    # IMPORTANT (FR-011, rebuilt-image evidence 2026-06-07): omitting wire_api does
    # NOT make codex default to chat-completions — codex 0.137.0 is hard-locked to the
    # OpenAI Responses API at request time and still POSTs to /responses. This removal
    # is NECESSARY (fixes startup) but INSUFFICIENT: until the DualModelProxy serves a
    # /responses front door, proxied codex still 404s at /responses. Do not assume this
    # alone makes codex work dual-model. Direct/non-proxy codex runs keep their
    # configured wire_api=responses — this override fires only here and reverts on stop().
    if backend == "codex":
        proxy.env_restores.append((target, "CODEX_PROVIDER_WIRE_API", target.get("CODEX_PROVIDER_WIRE_API")))
        target.pop("CODEX_PROVIDER_WIRE_API", None)
    log.info(
        "dual_model_proxy.launched",
        backend=backend, env_var=env_var, strategy=orchestration.get("strategy"),
    )
    return proxy

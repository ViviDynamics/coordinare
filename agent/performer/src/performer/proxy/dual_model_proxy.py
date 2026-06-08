"""080 — DualModelProxy: the in-container transport shell.

A same-process aiohttp reverse proxy (modeled on the 073 ``ClaudeCodeShim``)
bound to ``127.0.0.1:<ephemeral>``. The performer points the agent CLI's
provider base URL at it when the resolved mode strategy is not ``single``. For
each completion the CLI sends, the proxy normalizes the body to an
``LLMRequest``, runs the configured ``OrchestrationStrategy``, and renders the
merged result back to the CLI's wire format.

Security (inherited 073): the upstream auth token and request/response bodies
MUST NOT appear in logs; INFO is limited to method/path/status/latency.

This module keeps the orchestration glue (``build_strategy`` / ``run_completion``)
as pure, network-free functions so they unit-test directly; the aiohttp server
is a thin wrapper around them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import structlog

from performer.proxy.assembler import assemble_json, assemble_sse
from performer.proxy.classifier import ModelClassifier
from performer.proxy.llm_turn import LLMRequest
from performer.proxy.strategies import (
    DEFAULT_ERROR_PATTERN,
    AlwaysThinkThenAct,
    ConditionalEscalation,
    OrchestrationStrategy,
    SingleStrategy,
    StageState,
    ThinkOnceActMany,
)
from performer.proxy.upstreams import (
    HttpUpstream,
    to_llm_request_anthropic,
    to_llm_request_openai,
    to_llm_request_responses,
)

log = structlog.get_logger(__name__)

# CLI front-door paths → wire format (FR-010, FR-011). Both the ``/v1``-prefixed and
# bare forms are served: LiteLLM/OpenAI clients use ``/v1/chat/completions`` but
# several CLIs (opencode/openclaw) call ``/chat/completions`` directly, and
# Anthropic-style clients likewise split on ``/v1/messages`` vs ``/messages``. codex
# 0.137.0 is hard-locked to the OpenAI **Responses** API at request time — it POSTs
# to ``/responses`` regardless of provider ``wire_api`` config — so that path is
# served as a third wire format. The body shape is identical across the prefix
# variants, so each pair maps to the same wire format.
_PATH_WIRE = {
    "/v1/messages": "anthropic",
    "/messages": "anthropic",
    "/v1/chat/completions": "openai",
    "/chat/completions": "openai",
    "/v1/responses": "responses",
    "/responses": "responses",
}


def build_upstream(ref: dict[str, Any], *, client=None) -> HttpUpstream:
    """Build an HttpUpstream from a resolved UpstreamRef dispatch dict.

    ``ref`` carries {base_url?, model, wire_format, auth_env?, auth_style?}; the
    token is read from ``os.environ[auth_env]`` (never passed as a value).
    """
    auth_env = ref.get("auth_env")
    token = os.environ.get(auth_env, "") if auth_env else None
    return HttpUpstream(
        name=ref.get("name", ref.get("model", "upstream")),
        model=ref["model"],
        wire_format=ref.get("wire_format", "openai"),
        base_url=ref.get("base_url"),
        auth_token=token or None,
        auth_style=ref.get("auth_style", "bearer"),
        client=client,
    )


def build_strategy(orchestration: dict[str, Any], *, client=None) -> OrchestrationStrategy:
    """Construct the OrchestrationStrategy from a dispatch ``orchestration`` block."""
    strategy = orchestration["strategy"]
    if strategy not in ("single", "always", "conditional", "think_once"):
        raise ValueError(f"unknown orchestration strategy: {strategy!r}")
    tool = build_upstream(orchestration["tool"], client=client)
    on_err = orchestration.get("on_think_error", "fall_back_to_act")
    if strategy == "single":
        return SingleStrategy(tool=tool)
    thinking = build_upstream(orchestration["thinking"], client=client)
    if strategy == "always":
        return AlwaysThinkThenAct(thinking=thinking, tool=tool, on_think_error=on_err)
    if strategy == "conditional":
        classifier = ModelClassifier(build_upstream(orchestration["classifier"], client=client))
        return ConditionalEscalation(
            thinking=thinking, tool=tool, classifier=classifier,
            threshold=orchestration.get("threshold", 0.6), on_think_error=on_err,
        )
    if strategy == "think_once":
        state = StageState(
            invalidate_after_turns=orchestration.get("invalidate_after_turns"),
            invalidate_on_error=bool(orchestration.get("invalidate_on_error")),
            error_pattern=orchestration.get("error_pattern") or DEFAULT_ERROR_PATTERN,
        )
        return ThinkOnceActMany(thinking=thinking, tool=tool, state=state, on_think_error=on_err)
    raise ValueError(f"unknown orchestration strategy: {strategy!r}")


def _parse_request(body: dict[str, Any], wire_format: str) -> LLMRequest:
    if wire_format == "anthropic":
        return to_llm_request_anthropic(body)
    if wire_format == "responses":
        return to_llm_request_responses(body)
    return to_llm_request_openai(body)


async def run_completion(
    body: dict[str, Any], wire_format: str, strategy: OrchestrationStrategy, expose_plan_as: str,
) -> dict[str, Any]:
    """Pure orchestration glue (JSON): parse → run strategy → assemble. No network of its own."""
    request = _parse_request(body, wire_format)
    response, _record = await strategy.run(request)
    return assemble_json(response, expose_plan_as=expose_plan_as, wire_format=wire_format)


async def run_completion_sse(
    body: dict[str, Any], wire_format: str, strategy: OrchestrationStrategy, expose_plan_as: str,
) -> list[str]:
    """Orchestration glue (SSE): think runs internally; the assembled result is
    synthesized into an ordered list of SSE event blocks (plan events first)."""
    request = _parse_request(body, wire_format)
    response, _record = await strategy.run(request)
    return assemble_sse(response, expose_plan_as=expose_plan_as, wire_format=wire_format)


@dataclass
class DualModelProxy:
    """aiohttp reverse-proxy shell. ``start()`` binds 127.0.0.1:0 and returns the
    loopback base URL the CLI's provider base URL is set to.

    A single proxy serves one job/stage; the strategy (and its think_once
    StageState) persists for the proxy's lifetime.
    """

    orchestration: dict[str, Any]
    expose_plan_as: str = "thinking"
    client: Any = None  # optional shared httpx client (tests inject a MockTransport)
    # env mutations the launcher applied, restored on stop() so a long-lived
    # performer process doesn't leak a dead proxy URL into the next job's env.
    # Each entry is (mapping, key, prior_value); prior_value None means "was absent".
    env_restores: list = field(default_factory=list)
    _runner: Any = None
    _strategy: OrchestrationStrategy | None = None
    _owns_client: bool = False

    async def start(self) -> str:
        from aiohttp import web

        # One shared httpx client for the proxy's lifetime (reused across every
        # think/act/classifier call) instead of one per upstream call. Tests
        # inject their own client; we only create (and own) one when none given.
        if self.client is None:
            import httpx

            self.client = httpx.AsyncClient()
            self._owns_client = True
        self._strategy = build_strategy(self.orchestration, client=self.client)

        async def handle(request: "web.Request") -> "web.Response":
            import time

            wire = _PATH_WIRE.get(request.path, "openai")
            t0 = time.monotonic()
            try:
                body = await request.json()
                if bool(body.get("stream")):
                    events = await run_completion_sse(body, wire, self._strategy, self.expose_plan_as)
                    status = 200
                    resp = web.StreamResponse(
                        status=200,
                        headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
                    )
                    await resp.prepare(request)
                    for ev in events:
                        await resp.write(ev.encode())
                    await resp.write_eof()
                    return resp
                out = await run_completion(body, wire, self._strategy, self.expose_plan_as)
                status = 200
                return web.json_response(out)
            except Exception:  # noqa: BLE001 — never leak bodies; surface a clean 502
                status = 502
                return web.json_response({"error": {"message": "proxy orchestration failed"}}, status=502)
            finally:
                # log path/status/latency only — never bodies or auth (FR-019)
                log.info(
                    "dual_model_proxy.request",
                    path=request.path, status=status,
                    latency_ms=round((time.monotonic() - t0) * 1000, 1),
                )

        app = web.Application()
        for path in _PATH_WIRE:
            app.router.add_post(path, handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        sockets = self._runner.addresses
        host, port = sockets[0][0], sockets[0][1]
        return f"http://{host}:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None
            self._owns_client = False
        # restore env vars the launcher mutated (reverse order; last write wins)
        for mapping, key, prior in reversed(self.env_restores):
            if prior is None:
                mapping.pop(key, None)
            else:
                mapping[key] = prior
        self.env_restores = []

"""078 — SelfHostedShim: the normalize-strategy transport shell.

A same-process aiohttp reverse proxy (generalizing the 073 ``ClaudeCodeShim``
and modeled on the 080 ``DualModelProxy`` shell) bound to
``127.0.0.1:<ephemeral>``. When a ``(backend, model)`` pair resolves to a
``normalize`` :class:`~performer.proxy.routing.TargetDescriptor`, the launcher
points the agent CLI's provider base URL at this shim. The shim forwards each
request transparently to ``target.base_url`` and runs the target's declared,
format-keyed normalizers over the response — on BOTH the non-streaming JSON path
and the streaming SSE path.

Unlike ``DualModelProxy`` (which runs a multi-model orchestration *strategy*),
this shim is a **transparent reverse proxy**: it does not re-render or re-plan a
request, it forwards the CLI's body verbatim and only rewrites the *response*
through the normalizers. With zero normalizers it is a byte-for-byte pass-through.

Normalizer application is fail-open (FR-078-9): a normalizer that does not
recognize the response shape returns it unchanged, so an unexpected format flows
through rather than being dropped. Health gating (``health.py``) is the only
fail-*closed* surface in the layer.

Security (inherited 073/080): the upstream auth token and request/response
bodies MUST NOT appear in logs; INFO is limited to method/path/status/latency
plus the normalizer decision (which keys ran) — never tokens or bodies
(FR-078-10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from .normalizers.base import Normalizer, StatefulSSEFilter
    from .routing import TargetDescriptor

log = structlog.get_logger(__name__)

# CLI front-door paths the shim accepts (mirrors 080 ``_PATH_WIRE``). The CLI's
# provider base URL is set to the loopback origin, so it appends the standard
# ``/v1/...`` path; the shim forwards that same path to ``target.base_url``
# (which is therefore the upstream *origin*, no ``/v1`` of its own).
_FRONT_DOOR_PATHS = ("/v1/messages", "/v1/chat/completions")

# Per-request forwarding timeout for the shim's owned client. Bounds a wedged
# self-hosted upstream (the 077 spark/qwen runner-wedge edge) so a mid-job hang
# surfaces as a clean 502 rather than stalling the card indefinitely. The read
# timeout applies *between* streamed chunks, so it also detects a stall that
# begins mid-stream — not just a connect/first-byte hang. Only the shim's OWNED
# client gets this; an injected client (tests) keeps its own configuration.
_DEFAULT_FORWARD_TIMEOUT = 300.0
_CONNECT_TIMEOUT = 10.0

# Hop-by-hop headers we must not forward to the upstream (RFC 7230 §6.1) plus
# the inbound Host (the upstream sets its own) and content-length (httpx
# recomputes it). Everything else — including the CLI's auth header — passes
# through unchanged so the shim is a transparent reverse proxy.
_HOP_BY_HOP = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "accept-encoding",
    }
)


class _FilterChain:
    """Run a sequence of :class:`StatefulSSEFilter` over one SSE response stream.

    Each filter buffers across chunk boundaries independently; the output of one
    filter's ``feed`` is fed into the next, so normalizers compose. With an empty
    chain this is an identity transform (pass-through).
    """

    def __init__(self, filters: list["StatefulSSEFilter"]) -> None:
        self._filters = filters

    def feed(self, chunk: bytes) -> bytes:
        data = chunk
        for f in self._filters:
            data = f.feed(data)
            if not data:
                return b""
        return data

    def flush(self) -> bytes:
        # Drain each filter in order. A filter's flushed tail (plus any complete
        # frames it emits while consuming upstream residue) becomes the residue
        # fed into the next filter; the final residue is the stream's tail.
        residue = b""
        for f in self._filters:
            emitted = f.feed(residue) if residue else b""
            residue = emitted + f.flush()
        return residue


@dataclass
class SelfHostedShim:
    """aiohttp reverse-proxy shell for the ``normalize`` strategy.

    ``start()`` binds ``127.0.0.1:0`` and returns the loopback base URL the
    launcher sets as the backend's provider base URL. ``stop()`` tears the
    server down and restores any env the launcher mutated.
    """

    target: "TargetDescriptor"
    # resolved Normalizer instances (looked up from NORMALIZER_REGISTRY by the
    # launcher via target.normalizers); applied in declared order.
    normalizers: list["Normalizer"] = field(default_factory=list)
    client: Any = None  # optional shared httpx client (tests inject MockTransport)
    # per-request forwarding timeout (seconds) applied only to the shim's OWNED
    # client; bounds a wedged upstream so a mid-job hang surfaces as a 502.
    forward_timeout: float = _DEFAULT_FORWARD_TIMEOUT
    # env mutations the launcher applied, restored on stop() so a long-lived
    # performer process doesn't leak a dead shim URL into the next job's env.
    # Each entry is (mapping, key, prior_value); prior_value None means "absent".
    env_restores: list = field(default_factory=list)
    _runner: Any = None
    _owns_client: bool = False

    @property
    def _normalizer_keys(self) -> list[str]:
        return [n.key for n in self.normalizers]

    def _upstream_url(self, path_qs: str) -> str:
        """Join the target origin with the CLI's request path (transparent forward)."""
        return self.target.base_url.rstrip("/") + path_qs

    def _forward_headers(self, headers: Any) -> dict[str, str]:
        return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}

    def normalize_json(self, body: dict[str, Any]) -> dict[str, Any]:
        """Run every declared normalizer's JSON path in order (fail-open)."""
        for n in self.normalizers:
            body = n.normalize_json(body)
        return body

    def sse_chain(self) -> _FilterChain:
        """A fresh filter chain for one SSE response stream."""
        return _FilterChain([n.sse_filter() for n in self.normalizers])

    async def start(self) -> str:
        from aiohttp import web

        if self.client is None:
            import httpx

            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.forward_timeout, connect=_CONNECT_TIMEOUT)
            )
            self._owns_client = True

        async def handle(request: "web.Request") -> "web.StreamResponse":
            import time

            t0 = time.monotonic()
            status = 502
            try:
                raw = await request.read()
                upstream_url = self._upstream_url(request.path_qs)
                fwd_headers = self._forward_headers(request.headers)

                # Detect streaming the same way 080 does — inspect the request body.
                want_stream = False
                try:
                    import json as _json

                    want_stream = bool(_json.loads(raw or b"{}").get("stream"))
                except (ValueError, TypeError):
                    want_stream = False

                if want_stream:
                    status, resp = await self._proxy_sse(
                        request, upstream_url, fwd_headers, raw
                    )
                    return resp
                status, resp = await self._proxy_json(upstream_url, fwd_headers, raw)
                return resp
            except Exception:  # noqa: BLE001 — never leak bodies; surface a clean 502
                status = 502
                return web.json_response(
                    {"error": {"message": "self-hosted shim proxy failed"}}, status=502
                )
            finally:
                # method/path/status/latency + which normalizers ran — never
                # tokens or bodies (FR-078-10).
                log.info(
                    "selfhosted_shim.request",
                    method=request.method,
                    path=request.path,
                    status=status,
                    latency_ms=round((time.monotonic() - t0) * 1000, 1),
                    normalizers=self._normalizer_keys,
                )

        app = web.Application()
        for path in _FRONT_DOOR_PATHS:
            app.router.add_post(path, handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        host, port = self._runner.addresses[0][0], self._runner.addresses[0][1]
        return f"http://{host}:{port}"

    async def _proxy_json(
        self, upstream_url: str, headers: dict[str, str], raw: bytes
    ) -> tuple[int, Any]:
        from aiohttp import web

        resp = await self.client.post(upstream_url, content=raw, headers=headers)
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            # Non-JSON upstream body — pass through verbatim (fail-open).
            return status, web.Response(
                body=resp.content,
                status=status,
                content_type=resp.headers.get("content-type", "application/json"),
            )
        if isinstance(body, dict):
            body = self.normalize_json(body)
        return status, web.json_response(body, status=status)

    async def _proxy_sse(
        self,
        request: "Any",
        upstream_url: str,
        headers: dict[str, str],
        raw: bytes,
    ) -> tuple[int, Any]:
        from aiohttp import web

        chain = self.sse_chain()
        async with self.client.stream(
            "POST", upstream_url, content=raw, headers=headers
        ) as upstream:
            out_status = upstream.status_code
            if out_status != 200:
                # A non-200 upstream that still "streams" is an error (429/5xx),
                # not an SSE body. Surface the real status with the upstream's
                # error body verbatim (fail-open) rather than forcing a 200 with
                # a garbled/empty stream — that 200-over-an-error masquerade is
                # exactly the 077 "empty/garbage" failure shape this layer exists
                # to eliminate. We buffer here because the body is a short error,
                # not a long token stream.
                body = await upstream.aread()
                return out_status, web.Response(
                    body=body,
                    status=out_status,
                    content_type=upstream.headers.get(
                        "content-type", "application/json"
                    ),
                )

            out = web.StreamResponse(
                status=out_status,
                headers={
                    "content-type": "text/event-stream",
                    "cache-control": "no-cache",
                },
            )
            await out.prepare(request)
            async for chunk in upstream.aiter_bytes():
                transformed = chain.feed(chunk)
                if transformed:
                    await out.write(transformed)
            tail = chain.flush()
            if tail:
                await out.write(tail)
        await out.write_eof()
        return out_status, out

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

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

import os
import uuid
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
# Front-door POST routes the shim serves. Both the canonical ``/v1/...`` paths and
# the no-``/v1`` variants are registered: an OpenAI-wire CLI whose (repointed)
# provider base_url is the bare loopback origin POSTs to ``/chat/completions`` (no
# ``/v1``), which would otherwise 404 at the router before the handler ran (122:
# the shared cause of the openclaw + opencode shim failures). ``_upstream_url``
# canonicalizes whichever variant arrives to the upstream's expected ``/v1`` path.
_FRONT_DOOR_PATHS = (
    "/v1/messages", "/v1/chat/completions", "/messages", "/chat/completions",
)

# Per-request forwarding timeout for the shim's owned client. Bounds a wedged
# self-hosted upstream (the 077 local/qwen runner-wedge edge) so a mid-job hang
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
    },
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
    _session_id: str = field(default_factory=lambda: str(uuid.uuid4()), init=False)

    @property
    def _normalizer_keys(self) -> list[str]:
        return [n.key for n in self.normalizers]

    @property
    def _translate(self) -> bool:
        """True when this shim must translate Anthropic↔OpenAI wire (spec 084)."""
        return getattr(self.target, "strategy", None) == "translate"

    def _upstream_url(self, path_qs: str) -> str:
        """Join the target origin with the request path (transparent forward).

        In translate mode the Anthropic front door ``/v1/messages`` is rewritten
        to the OpenAI upstream path ``/v1/chat/completions`` (Decision 4); query
        string, if any, is preserved.

        084 / Ollama-compat: when ``base_url`` already ends with ``/v1`` (e.g.
        ``http://192.0.2.10:11434/v1``) the translated path ``/v1/chat/completions``
        must NOT be appended verbatim — that would produce the double-prefix
        ``…/v1/v1/chat/completions`` (Ollama 404). Strip the leading ``/v1``
        from the path when the base already carries it, matching the same
        Ollama-compat logic in ``health._probe_url``.
        """
        base = self.target.base_url.rstrip("/")
        path, sep, query = path_qs.partition("?")
        # Canonicalize the front-door path to the upstream's expected /v1 path.
        # A CLI whose (repointed) provider base_url lacks /v1 POSTs to
        # ``/chat/completions`` or ``/messages``; the upstream (LiteLLM/Ollama)
        # serves them under ``/v1/...``. Translate additionally rewrites the
        # Anthropic front door to the OpenAI upstream path (Decision 4).
        if self._translate and path in ("/v1/messages", "/messages"):
            path = "/v1/chat/completions"
        elif path in ("/chat/completions", "/v1/chat/completions"):
            path = "/v1/chat/completions"
        elif path in ("/messages", "/v1/messages"):
            path = "/v1/messages"
        # Avoid double-/v1 when base already ends with it (e.g. an Ollama base
        # ``…:11434/v1`` + ``/v1/chat/completions`` → ``…/v1/v1/…`` 404).
        if base.endswith("/v1") and path.startswith("/v1/"):
            path = path[3:]  # strip leading /v1
        return base + path + sep + query

    def _translate_request_bytes(self, raw: bytes) -> bytes:
        """Translate an inbound Anthropic ``/v1/messages`` body to OpenAI wire.

        Returns the re-serialized OpenAI body. A non-JSON / unparseable body is
        forwarded verbatim (the upstream will reject it) rather than dropped.
        Pure translation — no body content is logged (FR-011).
        """
        import json as _json

        from .translate import translate_request

        try:
            anthropic_body = _json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return raw
        if not isinstance(anthropic_body, dict):
            return raw
        openai_body = translate_request(anthropic_body)
        # 084: if the routing entry declares an ``upstream_model``, rewrite the
        # model name in the translated request so the OpenAI-wire upstream
        # (e.g. Ollama) receives its own model name rather than the Anthropic
        # model name the CLI was started with (which would be unknown to Ollama).
        upstream_model = getattr(self.target, "upstream_model", None)
        if upstream_model:
            openai_body["model"] = upstream_model
        return _json.dumps(openai_body).encode("utf-8")

    def _forward_headers(self, headers: Any) -> dict[str, str]:
        fwd = {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}
        # 122: when the target opts into upstream auth injection, override the
        # CLI's Authorization with the coordinare-owned key (from the named env
        # var). Some backend CLIs (junie/pi/hermes) fail to put the LiteLLM key on
        # the wire; this guarantees the upstream sees a valid Bearer. The secret is
        # read from the env var NAME on the descriptor — never logged. Drop any
        # existing Authorization (case-insensitively) before setting the canonical
        # one so a stale/blank CLI header can't shadow it.
        auth_env = getattr(self.target, "upstream_auth_env", None)
        if auth_env:
            token = (os.environ.get(auth_env) or "").strip()
            if token:
                # Drop EVERY auth-bearing header a CLI might set (Authorization,
                # api-key, x-api-key) — some backends (hermes) send the key via
                # ``api-key`` rather than ``Authorization``, and LiteLLM reads
                # whichever is present, so a stale one would shadow the injection.
                fwd = {
                    k: v for k, v in fwd.items()
                    if k.lower() not in ("authorization", "api-key", "x-api-key")
                }
                fwd["Authorization"] = f"Bearer {token}"
        session_header = self.target.upstream_session_header
        if session_header is not None:
            fwd = {k: v for k, v in fwd.items() if k.lower() != session_header}
            fwd[session_header] = self._session_id
        return fwd

    def normalize_raw(self, raw: bytes) -> bytes:
        """Run every declared normalizer's raw-bytes pre-parse path in order.

        098 US2: a normalizer may expose ``normalize_raw(bytes) -> bytes`` to
        repair a body *before* JSON parsing (e.g. strip invalid control bytes
        that would otherwise break ``json.loads`` and force a verbatim passthrough
        of the broken body to the harness). Fail-open and routing-scoped — only
        chains that declare such a normalizer are affected; all others no-op.
        """
        for n in self.normalizers:
            fn = getattr(n, "normalize_raw", None)
            if fn is not None:
                raw = fn(raw)
        return raw

    def normalize_json(self, body: dict[str, Any]) -> dict[str, Any]:
        """Run every declared normalizer's JSON path in order (fail-open).

        In translate mode the already-normalized OpenAI body is then wire-format
        translated to an Anthropic ``/v1/messages`` response as the OUTERMOST step
        (Decision 4: normalizers → wire translation).
        """
        for n in self.normalizers:
            body = n.normalize_json(body)
        if self._translate:
            from .translate import translate_response

            body = translate_response(body)
        return body

    def sse_chain(self) -> _FilterChain:
        """A fresh filter chain for one SSE response stream.

        In translate mode the OpenAI→Anthropic SSE translator is appended as the
        OUTERMOST filter, after the normalizers (Decision 4) — it consumes an
        already-reassembled OpenAI chunk stream and restructures it into the
        Anthropic event protocol.
        """
        filters = [n.sse_filter() for n in self.normalizers]
        if self._translate:
            from .translate import TranslatingSSEFilter

            filters.append(TranslatingSSEFilter())
        return _FilterChain(filters)

    async def start(self) -> str:
        from aiohttp import web

        if self.client is None:
            import httpx

            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.forward_timeout, connect=_CONNECT_TIMEOUT),
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

                # Detect streaming the same way 080 does — inspect the request
                # body. ``stream`` is a passthrough field in both wires, so the
                # original (pre-translation) body is authoritative.
                want_stream = False
                try:
                    import json as _json

                    want_stream = bool(_json.loads(raw or b"{}").get("stream"))
                except (ValueError, TypeError):
                    want_stream = False

                # Translate the inbound Anthropic body to OpenAI wire before
                # forwarding (translate strategy only; no-op otherwise).
                if self._translate:
                    raw = self._translate_request_bytes(raw)

                if want_stream:
                    status, resp = await self._proxy_sse(
                        request, upstream_url, fwd_headers, raw,
                    )
                    return resp
                status, resp = await self._proxy_json(upstream_url, fwd_headers, raw)
                return resp
            except Exception:  # noqa: BLE001 — never leak bodies; surface a clean 502
                status = 502
                return web.json_response(
                    {"error": {"message": "self-hosted shim proxy failed"}}, status=502,
                )
            finally:
                # method/path/status/latency + which translator + which
                # normalizers ran — never tokens, bodies, or auth (FR-078-10 /
                # 084 FR-011). ``translate`` records that the wire translator ran;
                # the secret lives only in the env-var NAME elsewhere, never here.
                log.info(
                    "selfhosted_shim.request",
                    method=request.method,
                    path=request.path,
                    status=status,
                    latency_ms=round((time.monotonic() - t0) * 1000, 1),
                    translate=self._translate,
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
        self, upstream_url: str, headers: dict[str, str], raw: bytes,
    ) -> tuple[int, Any]:
        import json as _json

        from aiohttp import web

        resp = await self.client.post(upstream_url, content=raw, headers=headers)
        status = resp.status_code
        # FR-012: a non-2xx upstream is surfaced verbatim and the translator is
        # NOT invoked — translating an OpenAI error envelope into a fake Anthropic
        # message would mask the real failure. (Normalizers are fail-open and
        # only meaningful on a success body, so they are skipped too.)
        if self._translate and not (200 <= status < 300):
            return status, web.Response(
                body=resp.content,
                status=status,
                content_type=resp.headers.get("content-type", "application/json"),
            )
        # 098 US2: repair the raw body before parsing (e.g. strip invalid control
        # bytes that would break json.loads). Routing-scoped + fail-open: a no-op
        # unless a normalizer in this chain declares ``normalize_raw``.
        raw_body = self.normalize_raw(resp.content)
        try:
            body = _json.loads(raw_body)
        except ValueError:
            # Non-JSON upstream body even after raw repair — pass through verbatim
            # (fail-open). Emit the ORIGINAL bytes, not the stripped ones.
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
            "POST", upstream_url, content=raw, headers=headers,
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
                        "content-type", "application/json",
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

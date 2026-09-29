"""In-container response-translation shim for the LiteLLM proxy (spec 073).

The claude CLI 2.1.148 parser rejects `thinking` content blocks emitted by
non-Anthropic models routed through LiteLLM ("Content block not found").  This
shim is a same-process asyncio reverse proxy bound to ``127.0.0.1:<ephemeral>``
that forwards ``POST /v1/messages`` to the operator's LiteLLM proxy and strips
``thinking`` content blocks from both JSON responses and SSE event streams
before they reach the CLI.  All other paths pass through unmodified (bearer
auth applied).

Security (FR-004 / FR-011 / SC-004):
    - The operator bearer (``LITELLM_PROXY_AUTH_TOKEN``) MUST NOT appear in any
      log line.  Request/response bodies MUST NOT appear in any log line.
    - INFO log: method + path + upstream status + latency only.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import aiohttp
import structlog
from aiohttp import web

from performer.proxy.model_rewrite import rewrite_request_model

log = structlog.get_logger(__name__)

_MESSAGES_PATH = "/v1/messages"
# Headers we never forward upstream. ``authorization`` / ``x-api-key`` are
# replaced by the operator bearer (LITELLM_PROXY_AUTH_TOKEN) in ``_handle`` —
# any value the CLI sent is dropped and will not reach the upstream proxy.
_HOP_BY_HOP = frozenset({
    "host", "content-length", "connection", "transfer-encoding",
    "authorization", "x-api-key",
})


def _strip_thinking_from_message(message: dict[str, Any]) -> None:
    """Remove ``thinking`` content blocks from a non-streaming JSON response.

    Mutates ``message["content"]`` in place when the field is a list.  No-op
    when the field is missing or not a list.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return
    message["content"] = [
        block for block in content
        if not (isinstance(block, dict) and block.get("type") == "thinking")
    ]


class _SSEThinkingFilter:
    """Stateful filter that strips ``thinking`` blocks from an SSE event stream.

    Anthropic streams send a sequence of events keyed by integer ``index``:
        content_block_start {index, content_block: {type: "thinking"|"text"|...}}
        content_block_delta {index, delta: {...}}
        content_block_stop  {index}

    We drop every event whose ``index`` started life as a ``thinking`` block,
    and renumber the indices of surviving blocks so they remain contiguous
    starting at zero (the CLI parser is index-sensitive).  ``message_start``,
    ``message_delta``, ``message_stop``, and ``ping`` events pass through
    unchanged.
    """

    def __init__(self) -> None:
        # original-index → "drop" or remapped-index
        self._drop: set[int] = set()
        self._remap: dict[int, int] = {}
        self._next_out_index = 0
        # SSE frames may span lines; we buffer raw bytes and emit complete frames.
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        """Append ``chunk`` to internal buffer; return filtered bytes ready to flush."""
        self._buf.extend(chunk)
        out = bytearray()
        while True:
            sep = self._buf.find(b"\n\n")
            if sep == -1:
                break
            frame = bytes(self._buf[:sep])
            del self._buf[:sep + 2]
            filtered = self._process_frame(frame)
            if filtered is not None:
                out.extend(filtered)
                out.extend(b"\n\n")
        return bytes(out)

    def flush(self) -> bytes:
        """Return any trailing partial frame (no separator seen)."""
        if not self._buf:
            return b""
        tail = bytes(self._buf)
        self._buf.clear()
        # A trailing frame without `\n\n` is malformed per SSE, but pass it
        # through verbatim rather than swallowing data.
        return tail

    def _process_frame(self, frame: bytes) -> bytes | None:
        event_name: str | None = None
        data_lines: list[str] = []
        for line in frame.split(b"\n"):
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                return frame  # pass through opaque binary
            if text.startswith("event:"):
                event_name = text[6:].strip()
            elif text.startswith("data:"):
                data_lines.append(text[5:].lstrip())
        if not data_lines or event_name is None:
            return frame
        data_str = "\n".join(data_lines)
        try:
            payload = json.loads(data_str)
        except json.JSONDecodeError:
            return frame
        if not isinstance(payload, dict):
            return frame
        etype = payload.get("type") or event_name
        idx = payload.get("index")

        if etype == "content_block_start" and isinstance(idx, int):
            block = payload.get("content_block") or {}
            if isinstance(block, dict) and block.get("type") == "thinking":
                self._drop.add(idx)
                return None
            new_idx = self._next_out_index
            self._next_out_index += 1
            self._remap[idx] = new_idx
            payload["index"] = new_idx
            return _encode_sse(event_name, payload)

        if etype in ("content_block_delta", "content_block_stop") and isinstance(idx, int):
            if idx in self._drop:
                return None
            if idx in self._remap:
                payload["index"] = self._remap[idx]
                return _encode_sse(event_name, payload)
            # Unknown index — pass through unchanged.
            return frame

        # message_start / message_delta / message_stop / ping / error — pass through.
        return frame


def _encode_sse(event_name: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}".encode("utf-8")


class ClaudeCodeShim:
    """asyncio reverse proxy that normalizes responses for the claude CLI parser.

    Bound to ``127.0.0.1:<ephemeral_port>``.  Lifecycle is owned by the
    ``ClaudeCodeBackend``: call ``start()`` before launching the subprocess
    and ``stop()`` after it exits.
    """

    def __init__(
        self,
        upstream_base_url: str,
        auth_token: str,
        *,
        client_session: aiohttp.ClientSession | None = None,
        capture_dir: str = "",
    ) -> None:
        self._upstream = upstream_base_url.rstrip("/")
        self._token = auth_token
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._session: aiohttp.ClientSession | None = client_session
        self._owns_session = client_session is None
        self._base_url: str | None = None
        self._capture_dir = capture_dir
        self._capture_seq = 0
        self._capture_lock = asyncio.Lock()
        if self._capture_dir:
            os.makedirs(self._capture_dir, exist_ok=True)
            # Capture files persist raw request/response bytes including any
            # Bearer tokens the upstream might echo on error. Lock to 0700 so
            # other users on a shared host can't read them. Debug-only path
            # per FR-011 — operators must opt in via LITELLM_PROXY_CAPTURE_DIR.
            try:
                os.chmod(self._capture_dir, 0o700)
            except OSError:  # pragma: no cover — best-effort hardening
                pass

    @property
    def base_url(self) -> str:
        if self._base_url is None:
            raise RuntimeError("shim not started")
        return self._base_url

    async def start(self) -> str:
        """Bind to an ephemeral loopback port and return the base URL."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        # Discover the ephemeral port assigned by the OS.
        server = site._server  # type: ignore[attr-defined]
        sockets = server.sockets if server is not None else ()
        if not sockets:
            await runner.cleanup()
            raise RuntimeError("shim failed to bind a loopback socket")
        port = sockets[0].getsockname()[1]
        self._app = app
        self._runner = runner
        self._site = site
        self._base_url = f"http://127.0.0.1:{port}"
        log.info("claude_code shim listening", port=port)
        return self._base_url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._app = None
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None
        self._base_url = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        started = time.monotonic()
        assert self._session is not None
        method = request.method
        path = request.match_info.get("path", "")
        # Ollama-compat (mirrors SelfHostedShim._upstream_url in proxy/shim.py):
        # when the upstream base URL already ends with ``/v1`` (e.g.
        # LITELLM_PROXY_BASE_URL=…:4000/v1), strip the leading ``v1/`` from the
        # CLI's request path so the join below yields a single ``/v1`` — the
        # verbatim join produced ``…:4000/v1/v1/messages`` → 404 → zero commits
        # (cluster E2E Round 10, 2026-09-27).
        upstream_base = self._upstream
        if upstream_base.endswith("/v1") and path.startswith("v1/"):
            path = path[3:]
        url = f"{upstream_base}/{path}"
        if request.query_string:
            url = f"{url}?{request.query_string}"

        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        headers["Authorization"] = f"Bearer {self._token}"

        body = await request.read()
        normalize = (method == "POST" and request.path == _MESSAGES_PATH)
        capture_prefix = await self._capture_prefix(request.path) if normalize else None
        if capture_prefix is not None:
            _write_capture(f"{capture_prefix}.req.json", body)
            _write_capture(
                f"{capture_prefix}.req.headers.json",
                _redacted_headers_json(headers),
            )
        # 496: the CLI has a background call path that requests its hardcoded
        # small model (claude-haiku-*) without consulting ANTHROPIC_SMALL_FAST_MODEL.
        # Rewrite that family to the configured model before it reaches the
        # upstream; applied AFTER capture so the diagnostic file shows what the
        # CLI actually emitted. Fail-open: unparseable bodies forward verbatim.
        if normalize:
            body = rewrite_request_model(body, os.environ)

        try:
            upstream = await self._session.request(
                method, url, headers=headers, data=body, allow_redirects=False,
            )
        except aiohttp.ClientError as exc:
            log.warning("claude_code shim upstream error", path=request.path, error=type(exc).__name__)
            return web.Response(status=502, text="upstream error")

        try:
            content_type = upstream.headers.get("content-type", "")
            is_sse = "text/event-stream" in content_type.lower()

            if capture_prefix is not None:
                _write_capture(
                    f"{capture_prefix}.resp.headers.json",
                    _redacted_headers_json(dict(upstream.headers)),
                )
                _write_capture(
                    f"{capture_prefix}.resp.status",
                    str(upstream.status).encode("utf-8"),
                )

            if normalize and not is_sse and "application/json" in content_type.lower():
                payload_bytes = await upstream.read()
                if capture_prefix is not None:
                    _write_capture(f"{capture_prefix}.resp.json", payload_bytes)
                try:
                    parsed = json.loads(payload_bytes)
                    if isinstance(parsed, dict):
                        _strip_thinking_from_message(parsed)
                    out = json.dumps(parsed, separators=(",", ":")).encode("utf-8")
                except json.JSONDecodeError:
                    out = payload_bytes
                resp_headers = _forward_headers(upstream.headers)
                resp_headers["content-length"] = str(len(out))
                self._log_done(method, request.path, upstream.status, started)
                return web.Response(status=upstream.status, body=out, headers=resp_headers)

            if normalize and is_sse:
                resp = web.StreamResponse(
                    status=upstream.status, headers=_forward_headers(upstream.headers),
                )
                await resp.prepare(request)
                filt = _SSEThinkingFilter()
                raw_buf = bytearray() if capture_prefix is not None else None
                async for chunk in upstream.content.iter_any():
                    if raw_buf is not None:
                        raw_buf.extend(chunk)
                    out = filt.feed(chunk)
                    if out:
                        await resp.write(out)
                tail = filt.flush()
                if tail:
                    await resp.write(tail)
                await resp.write_eof()
                if raw_buf is not None and capture_prefix is not None:
                    _write_capture(f"{capture_prefix}.resp.sse", bytes(raw_buf))
                self._log_done(method, request.path, upstream.status, started)
                return resp

            # Passthrough — non-/v1/messages or non-JSON/SSE bodies.
            resp = web.StreamResponse(
                status=upstream.status, headers=_forward_headers(upstream.headers),
            )
            await resp.prepare(request)
            async for chunk in upstream.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            self._log_done(method, request.path, upstream.status, started)
            return resp
        finally:
            upstream.release()

    async def _capture_prefix(self, path: str) -> str | None:
        """Return a per-request file prefix under capture_dir, or None if disabled."""
        if not self._capture_dir:
            return None
        async with self._capture_lock:
            self._capture_seq += 1
            seq = self._capture_seq
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        return os.path.join(self._capture_dir, f"{ts}-{seq:04d}")

    @staticmethod
    def _log_done(method: str, path: str, status: int, started: float) -> None:
        latency_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "claude_code shim request",
            method=method, path=path, status=status, latency_ms=latency_ms,
        )


_SENSITIVE_HEADERS = frozenset({"authorization", "x-api-key", "proxy-authorization", "cookie", "set-cookie"})


def _redacted_headers_json(headers: dict[str, str]) -> bytes:
    redacted = {
        k: ("<redacted>" if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }
    return json.dumps(redacted, indent=2).encode("utf-8")


def _write_capture(path: str, data: bytes) -> None:
    """Write capture bytes to disk; never raise into the request path."""
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        log.warning("claude_code shim capture write failed", error=type(exc).__name__)


def _forward_headers(upstream_headers: Any) -> dict[str, str]:
    """Copy upstream headers minus hop-by-hop entries the server will set itself."""
    skip = {"content-length", "transfer-encoding", "connection"}
    return {k: v for k, v in upstream_headers.items() if k.lower() not in skip}

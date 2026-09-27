"""073 T025/T028: unit tests for the claude_code response-translation shim.

Covers:
    - JSON ``thinking`` block stripping (US4 AS-1)
    - SSE ``thinking`` block stripping + index renumbering (US4 AS-2)
    - Non-/v1/messages passthrough (FR-009)
    - Secret hygiene (FR-011 / SC-004): bearer + request/response bodies MUST NOT
      appear in any structlog output.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
import pytest
import structlog
from aiohttp import web

from performer.backends.claude_code_shim import (
    ClaudeCodeShim,
    _SSEThinkingFilter,
    _strip_thinking_from_message,
)


# ---------------------------------------------------------------------------
# Pure-function tests — no I/O
# ---------------------------------------------------------------------------


class TestStripThinkingFromMessage:
    def test_removes_only_thinking_blocks(self) -> None:
        msg = {
            "content": [
                {"type": "thinking", "thinking": "deliberating...", "signature": None},
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "name": "read"},
            ]
        }
        _strip_thinking_from_message(msg)
        assert msg["content"] == [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "name": "read"},
        ]

    def test_no_op_when_content_missing(self) -> None:
        msg: dict[str, Any] = {"id": "msg_1"}
        _strip_thinking_from_message(msg)
        assert msg == {"id": "msg_1"}


class TestSSEThinkingFilter:
    @staticmethod
    def _frame(event: str, payload: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()

    def test_drops_thinking_blocks_and_renumbers(self) -> None:
        filt = _SSEThinkingFilter()
        # message_start passes through
        out = filt.feed(self._frame("message_start", {"type": "message_start"}))
        assert b"message_start" in out
        # content_block_start index=0 thinking — dropped
        out = filt.feed(self._frame(
            "content_block_start",
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
        ))
        assert out == b""
        # delta for index=0 — dropped
        out = filt.feed(self._frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "..."}},
        ))
        assert out == b""
        # stop for index=0 — dropped
        out = filt.feed(self._frame(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ))
        assert out == b""
        # content_block_start index=1 text — kept, renumbered to 0
        out = filt.feed(self._frame(
            "content_block_start",
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "text", "text": ""}},
        ))
        decoded = out.decode()
        assert '"index":0' in decoded
        assert "content_block_start" in decoded
        # delta for index=1 — kept, renumbered to 0
        out = filt.feed(self._frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "Hi"}},
        ))
        assert '"index":0' in out.decode()
        # stop for index=1 — kept, renumbered to 0
        out = filt.feed(self._frame(
            "content_block_stop", {"type": "content_block_stop", "index": 1}
        ))
        assert '"index":0' in out.decode()

    def test_text_only_stream_unchanged(self) -> None:
        filt = _SSEThinkingFilter()
        out = filt.feed(self._frame(
            "content_block_start",
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
        ))
        assert '"index":0' in out.decode()
        out = filt.feed(self._frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "hi"}},
        ))
        assert '"index":0' in out.decode()


# ---------------------------------------------------------------------------
# End-to-end tests with a fake upstream
# ---------------------------------------------------------------------------


@pytest.fixture
async def upstream() -> Any:
    """Spin up an aiohttp server acting as the upstream LiteLLM proxy.

    Bootstraps ``AppRunner`` directly (no pytest-aiohttp plugin needed).  Each
    test attaches a handler by mutating ``state['handler']``.
    """
    state: dict[str, Any] = {
        "handler": None, "received_auth": None, "received_body": None, "received_path": None,
    }

    async def _root(request: web.Request) -> web.StreamResponse:
        state["received_auth"] = request.headers.get("Authorization")
        state["received_body"] = await request.read()
        state["received_path"] = request.path
        handler = state["handler"]
        assert handler is not None, "test must set state['handler']"
        return await handler(request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _root)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    state["url"] = f"http://127.0.0.1:{port}"
    try:
        yield state
    finally:
        await runner.cleanup()


class TestUpstreamV1Join:
    """Regression (cluster E2E Round 10, 2026-09-27): /v1-suffixed base URLs.

    The 078 ``SelfHostedShim`` already strips the leading ``/v1`` from request
    paths when ``target.base_url`` ends with ``/v1`` (Ollama-compat).  The 073
    ``ClaudeCodeShim`` forwards verbatim and produced the live double-prefix
    failure ``…:4000/v1/v1/messages`` → 404 → zero commits → PR creation 422.
    """

    @staticmethod
    async def _post_and_capture_path(upstream: dict[str, Any], path: str) -> None:
        async def handler(_req: web.Request) -> web.Response:
            return web.json_response({"content": [{"type": "text", "text": "ok"}]})

        upstream["handler"] = handler
        shim = ClaudeCodeShim(f"{upstream['url']}/v1", "tok")
        base = await shim.start()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{base}{path}", json={}) as resp:
                    await resp.read()
        finally:
            await shim.stop()

    async def test_v1_suffixed_base_messages_not_doubled(
        self, upstream: dict[str, Any],
    ) -> None:
        await self._post_and_capture_path(upstream, "/v1/messages")
        assert upstream["received_path"] == "/v1/messages"

    async def test_v1_suffixed_base_count_tokens_not_doubled(
        self, upstream: dict[str, Any],
    ) -> None:
        await self._post_and_capture_path(upstream, "/v1/messages/count_tokens")
        assert upstream["received_path"] == "/v1/messages/count_tokens"

    async def test_v1_suffixed_base_non_v1_path_unchanged(
        self, upstream: dict[str, Any],
    ) -> None:
        """A path without a /v1 prefix keeps the verbatim join semantics."""
        await self._post_and_capture_path(upstream, "/foo")
        assert upstream["received_path"] == "/v1/foo"


class TestShimE2E:
    async def test_json_messages_strips_thinking(self, upstream: dict[str, Any]) -> None:
        async def handler(_req: web.Request) -> web.Response:
            body = {
                "id": "msg_1",
                "type": "message",
                "content": [
                    {"type": "thinking", "thinking": "secret reasoning text", "signature": None},
                    {"type": "text", "text": "Hello"},
                ],
            }
            return web.json_response(body)

        upstream["handler"] = handler
        shim = ClaudeCodeShim(upstream["url"], "sentinel-bearer-XYZ")
        base = await shim.start()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{base}/v1/messages",
                    json={"model": "x", "messages": []},
                ) as resp:
                    assert resp.status == 200
                    payload = await resp.json()
            assert payload["content"] == [{"type": "text", "text": "Hello"}]
            assert upstream["received_auth"] == "Bearer sentinel-bearer-XYZ"
        finally:
            await shim.stop()

    async def test_sse_messages_strips_thinking_events(self, upstream: dict[str, Any]) -> None:
        # Two blocks upstream: thinking (index 0) + text (index 1).
        async def handler(_req: web.Request) -> web.StreamResponse:
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(_req)
            frames = [
                ("message_start", {"type": "message_start", "message": {"id": "m"}}),
                ("content_block_start",
                 {"type": "content_block_start", "index": 0,
                  "content_block": {"type": "thinking", "thinking": ""}}),
                ("content_block_delta",
                 {"type": "content_block_delta", "index": 0,
                  "delta": {"type": "thinking_delta", "thinking": "private chain-of-thought"}}),
                ("content_block_stop",
                 {"type": "content_block_stop", "index": 0}),
                ("content_block_start",
                 {"type": "content_block_start", "index": 1,
                  "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta",
                 {"type": "content_block_delta", "index": 1,
                  "delta": {"type": "text_delta", "text": "Hi!"}}),
                ("content_block_stop",
                 {"type": "content_block_stop", "index": 1}),
                ("message_stop", {"type": "message_stop"}),
            ]
            for name, p in frames:
                await resp.write(
                    f"event: {name}\ndata: {json.dumps(p)}\n\n".encode()
                )
            await resp.write_eof()
            return resp

        upstream["handler"] = handler
        shim = ClaudeCodeShim(upstream["url"], "tok")
        base = await shim.start()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{base}/v1/messages", json={}) as resp:
                    body = (await resp.read()).decode()
            # Thinking events removed
            assert "thinking_delta" not in body
            assert "private chain-of-thought" not in body
            # Surviving text block renumbered to index 0
            assert '"text_delta"' in body
            assert '"index":0' in body
            # Original index=1 must not survive
            assert '"index":1' not in body
        finally:
            await shim.stop()

    async def test_non_messages_path_passthrough(self, upstream: dict[str, Any]) -> None:
        """FR-009: non-/v1/messages paths forwarded unmodified with bearer."""
        async def handler(_req: web.Request) -> web.Response:
            return web.json_response(
                {"content": [{"type": "thinking", "thinking": "kept"}]}
            )

        upstream["handler"] = handler
        shim = ClaudeCodeShim(upstream["url"], "tok")
        base = await shim.start()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{base}/v1/messages/count_tokens", json={}) as resp:
                    payload = await resp.json()
            # Thinking block preserved on passthrough path
            assert payload["content"][0]["type"] == "thinking"
            assert upstream["received_auth"] == "Bearer tok"
        finally:
            await shim.stop()


# ---------------------------------------------------------------------------
# T028: secret hygiene — token and body MUST NOT appear in logs
# ---------------------------------------------------------------------------


class TestSecretHygiene:
    async def test_bearer_and_body_not_logged(
        self,
        upstream: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def handler(_req: web.Request) -> web.Response:
            return web.json_response(
                {"content": [{"type": "text", "text": "ok"}]}
            )

        upstream["handler"] = handler
        # Route structlog through stdlib logging so caplog captures it.
        structlog.configure(
            processors=[structlog.processors.KeyValueRenderer()],
            logger_factory=structlog.stdlib.LoggerFactory(),
        )
        sentinel_token = "sk-shimtest-DO-NOT-LOG-9f8e7d"
        sentinel_body = "REQUEST_BODY_SENTINEL_a1b2c3"

        shim = ClaudeCodeShim(upstream["url"], sentinel_token)
        base = await shim.start()
        try:
            with caplog.at_level(logging.DEBUG):
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        f"{base}/v1/messages",
                        json={"prompt": sentinel_body},
                    ) as resp:
                        await resp.read()
        finally:
            await shim.stop()

        combined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert sentinel_token not in combined
        assert sentinel_body not in combined

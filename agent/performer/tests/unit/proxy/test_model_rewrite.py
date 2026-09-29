"""496 — the claude CLI's hardcoded small-model background calls must be rewritten.

Cluster-cluster E2E (release 2026.9.126, litellm proxy key restricted to an
allowed-model list): every successful turn produced exactly one litellm ERROR —
a background CLI call requested ``claude-haiku`` and the proxy key refused it.
``ANTHROPIC_MODEL`` and ``ANTHROPIC_SMALL_FAST_MODEL`` were both set and the
main model path had zero 400s, so exactly one call path in the CLI still
targets its hardcoded small model.

The fix seam is the request-normalization layer: a pure decision function
(which requests get rewritten, which are left alone) hosted in
``performer.proxy`` and wired into the 073 ``ClaudeCodeShim`` — the proxy the
claude CLI's traffic actually traverses in the observed failure.
"""
from __future__ import annotations

import json
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from performer.backends.claude_code_shim import ClaudeCodeShim
from performer.proxy.model_rewrite import (
    resolve_small_model_rewrite,
    rewrite_request_model,
)

_ENV = {
    "ANTHROPIC_SMALL_FAST_MODEL": "glm-5.3-flash",
    "ANTHROPIC_MODEL": "glm-5.3-flash",
}


class TestResolveSmallModelRewrite:
    def test_haiku_request_rewritten_to_configured_small_model(self) -> None:
        assert resolve_small_model_rewrite("claude-haiku-4-5-20251001", _ENV) == (
            "glm-5.3-flash"
        )

    def test_legacy_haiku_naming_is_rewritten(self) -> None:
        assert resolve_small_model_rewrite("claude-3-5-haiku-20241022", _ENV) == (
            "glm-5.3-flash"
        )

    def test_falls_back_to_primary_model_when_small_model_unset(self) -> None:
        env = {"ANTHROPIC_MODEL": "glm-5.3-flash"}
        assert resolve_small_model_rewrite("claude-haiku-4-5-20251001", env) == (
            "glm-5.3-flash"
        )

    def test_no_env_leaves_family_request_alone(self) -> None:
        assert resolve_small_model_rewrite("claude-haiku-4-5-20251001", {}) is None

    def test_blank_env_values_treated_as_unset(self) -> None:
        env = {"ANTHROPIC_SMALL_FAST_MODEL": "  ", "ANTHROPIC_MODEL": ""}
        assert resolve_small_model_rewrite("claude-haiku-4-5-20251001", env) is None

    def test_non_family_model_left_alone(self) -> None:
        assert resolve_small_model_rewrite("glm-5.3-flash", _ENV) is None

    def test_other_claude_models_left_alone(self) -> None:
        assert resolve_small_model_rewrite("claude-sonnet-4-5", _ENV) is None
        assert resolve_small_model_rewrite("claude-opus-4-1", _ENV) is None

    def test_requested_equal_to_target_is_a_no_op(self) -> None:
        env = {"ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5-20251001"}
        assert resolve_small_model_rewrite("claude-haiku-4-5-20251001", env) is None

    def test_empty_model_left_alone(self) -> None:
        assert resolve_small_model_rewrite("", _ENV) is None
        assert resolve_small_model_rewrite(None, _ENV) is None


class TestRewriteRequestModel:
    def test_rewrites_model_field_in_body(self) -> None:
        raw = json.dumps({"model": "claude-haiku-4-5-20251001", "messages": []}).encode()
        out = rewrite_request_model(raw, _ENV)
        assert json.loads(out)["model"] == "glm-5.3-flash"

    def test_non_family_body_bytes_unchanged(self) -> None:
        raw = json.dumps({"model": "glm-5.3-flash", "messages": []}).encode()
        assert rewrite_request_model(raw, _ENV) == raw

    def test_non_json_body_forwarded_verbatim(self) -> None:
        raw = b"not-json"
        assert rewrite_request_model(raw, _ENV) == raw


# ---------------------------------------------------------------------------
# ClaudeCodeShim wiring — the proxy the CLI's background call actually crosses.
# ---------------------------------------------------------------------------


@pytest.fixture
async def upstream() -> Any:
    state: dict[str, Any] = {"handler": None, "received_body": None, "received_path": None}

    async def _root(request: web.Request) -> web.StreamResponse:
        state["received_body"] = await request.read()
        state["received_path"] = request.path
        handler = state["handler"]
        assert handler is not None
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


class TestShimRewritesSmallModel:
    async def test_background_small_model_call_rewritten(
        self, upstream: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def handler(_req: web.Request) -> web.Response:
            return web.json_response({"content": [{"type": "text", "text": "ok"}]})

        upstream["handler"] = handler
        for key, value in _ENV.items():
            monkeypatch.setenv(key, value)
        shim = ClaudeCodeShim(upstream["url"], "tok")
        base = await shim.start()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{base}/v1/messages",
                    json={"model": "claude-haiku-4-5-20251001", "messages": []},
                ) as resp:
                    assert resp.status == 200
                    await resp.read()
        finally:
            await shim.stop()
        received = json.loads(upstream["received_body"])
        assert received["model"] == "glm-5.3-flash"

    async def test_main_model_call_forwarded_unchanged(
        self, upstream: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def handler(_req: web.Request) -> web.Response:
            return web.json_response({"content": [{"type": "text", "text": "ok"}]})

        upstream["handler"] = handler
        for key, value in _ENV.items():
            monkeypatch.setenv(key, value)
        body = {"model": "glm-5.3-flash", "messages": []}
        shim = ClaudeCodeShim(upstream["url"], "tok")
        base = await shim.start()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{base}/v1/messages", json=body) as resp:
                    await resp.read()
        finally:
            await shim.stop()
        received = json.loads(upstream["received_body"])
        assert received["model"] == "glm-5.3-flash"

    async def test_no_env_small_model_call_forwarded_unchanged(
        self, upstream: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def handler(_req: web.Request) -> web.Response:
            return web.json_response({"content": [{"type": "text", "text": "ok"}]})

        upstream["handler"] = handler
        monkeypatch.delenv("ANTHROPIC_SMALL_FAST_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        shim = ClaudeCodeShim(upstream["url"], "tok")
        base = await shim.start()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{base}/v1/messages",
                    json={"model": "claude-haiku-4-5-20251001", "messages": []},
                ) as resp:
                    await resp.read()
        finally:
            await shim.stop()
        received = json.loads(upstream["received_body"])
        assert received["model"] == "claude-haiku-4-5-20251001"

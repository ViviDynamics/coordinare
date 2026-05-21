"""Integration test for transparent upstream error surfacing (spec 067 T025).

Points ``OpenCodeCompatAdapter._post_chat_completions`` at a local
``httpx.MockTransport`` stub returning 404/400/503 in turn. Asserts each
response yields an ``upstream_http_error`` envelope whose body is verbatim
from the stub, and that ``classify_upstream`` tags 503 as transient while
400/404 are permanent (per FR-005 marker tuple SSOT).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from performer.backends.opencode_compat import OpenCodeCompatAdapter

from coordinare.graph.nodes.handle_system_error import classify_upstream
from coordinare.upstream_errors import UpstreamHTTPError

pytestmark = pytest.mark.integration


CASES = [
    (404, "model not found: qwen3-coder-30b", "permanent"),
    (400, "context length exceeded: 32768 < 65000", "permanent"),
    (503, "service unavailable", "transient"),
]


def _stub_transport(status: int, body: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions"), request.url
        # Verify body is LCD-shaped JSON so the validator path executed.
        payload = json.loads(request.content)
        assert "messages" in payload and "model" in payload
        return httpx.Response(status, text=body, headers={"x-request-id": "req-test"})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("status,body,verdict", CASES, ids=[f"{c[0]}" for c in CASES])
def test_transparent_upstream_error_envelope(status: int, body: str, verdict: str):
    adapter = OpenCodeCompatAdapter(base_url="http://stub.local/v1", api_key="test-key")

    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=_stub_transport(status, body),
            base_url=adapter.base_url,
        ) as client:
            return await adapter._post_chat_completions(
                {
                    "model": "qwen3-coder-30b",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                client=client,
            )

    response = asyncio.run(_run())
    assert response.status_code == status

    envelope_dict = adapter.last_upstream_http_error
    assert envelope_dict is not None
    # Verbatim body surfaced (no friendly rewrap).
    assert envelope_dict["body"] == body
    assert envelope_dict["body_truncated"] is False
    assert envelope_dict["status"] == status
    assert envelope_dict["upstream_request_id"] == "req-test"
    assert envelope_dict["base_url"] == "http://stub.local/v1"

    # Round-trip through the pydantic envelope and classify.
    envelope = UpstreamHTTPError(**{**envelope_dict, "occurred_at": envelope_dict["occurred_at"]})
    assert classify_upstream(envelope) == verdict


def test_2xx_response_does_not_set_envelope():
    adapter = OpenCodeCompatAdapter(base_url="http://stub.local/v1", api_key="test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=adapter.base_url
        ) as client:
            return await adapter._post_chat_completions(
                {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                client=client,
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert adapter.last_upstream_http_error is None


def test_2xx_after_prior_failure_clears_stale_envelope():
    """A subsequent successful call MUST clear the envelope from a prior 5xx —
    otherwise the orchestration layer can mis-attribute a stale failure to the
    new success.
    """
    adapter = OpenCodeCompatAdapter(base_url="http://stub.local/v1", api_key="test-key")

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    async def _run() -> tuple[httpx.Response, httpx.Response, dict | None]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=adapter.base_url
        ) as client:
            body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
            first = await adapter._post_chat_completions(body, client=client)
            after_failure = adapter.last_upstream_http_error
            second = await adapter._post_chat_completions(body, client=client)
            return first, second, after_failure

    first, second, after_failure = asyncio.run(_run())
    assert first.status_code == 503
    assert after_failure is not None and after_failure["status"] == 503
    assert second.status_code == 200
    assert adapter.last_upstream_http_error is None


def test_lcd_payload_error_logs_warn_before_raising():
    """A forbidden top-level key MUST raise LcdPayloadError AND emit a
    structured WARN naming the route, so operators can trace the rejection
    without a Python traceback.
    """
    import structlog
    from performer.backends.opencode_compat import LcdPayloadError

    adapter = OpenCodeCompatAdapter(base_url="http://stub.local/v1")

    async def _run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            base_url=adapter.base_url,
        ) as client:
            await adapter._post_chat_completions(
                {
                    "model": "x",
                    "messages": [{"role": "user", "content": "hi"}],
                    "prompt_cache_key": "leaked",
                },
                client=client,
            )

    with structlog.testing.capture_logs() as logs, pytest.raises(LcdPayloadError):
        asyncio.run(_run())

    rejection = [r for r in logs if r.get("event") == "opencode_compat.lcd_payload_rejected"]
    assert rejection, f"expected lcd_payload_rejected WARN; got: {logs}"
    assert rejection[0]["log_level"] == "warning"
    assert rejection[0]["route"] == "POST /v1/chat/completions"
    assert "prompt_cache_key" in rejection[0]["error"]


def test_base_url_path_preserved_on_wire():
    """base_url='http://host/v1' MUST hit /v1/chat/completions on the wire —
    not /chat/completions (RFC 3986 path-replacement bug fixed in spec 067).
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["full"] = str(request.url)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    adapter = OpenCodeCompatAdapter(base_url="http://stub.local/v1", api_key="k")

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter._post_chat_completions(
                {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                client=client,
            )

    asyncio.run(_run())
    assert captured["path"] == "/v1/chat/completions", captured
    assert captured["full"] == "http://stub.local/v1/chat/completions", captured


def test_base_url_with_trailing_slash_normalised():
    """A trailing slash on base_url must not produce '//chat/completions'."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    adapter = OpenCodeCompatAdapter(base_url="http://stub.local/v1/", api_key="k")

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter._post_chat_completions(
                {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                client=client,
            )

    asyncio.run(_run())
    assert captured["path"] == "/v1/chat/completions", captured


def test_missing_base_url_with_malformed_route_raises_clear_error():
    """When base_url is unset, the route arg must be 'METHOD /path' shaped —
    a malformed route should raise a clear ValueError, not IndexError.
    """
    # base_url stays None (default) — exercises the bogus-route guard.
    adapter = OpenCodeCompatAdapter()

    async def _run() -> None:
        await adapter._post_chat_completions(
            {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            route="bogus-route",
        )

    with pytest.raises(ValueError, match="METHOD /path"):
        asyncio.run(_run())

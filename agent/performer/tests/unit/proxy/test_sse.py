"""080 — SSE rendering (assemble_sse) + streaming through the proxy shell (FR-014)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from performer.proxy.assembler import assemble_sse
from performer.proxy.dual_model_proxy import DualModelProxy
from performer.proxy.llm_turn import LLMResponse, ToolCall

_RESP = LLMResponse(content="answer", tool_calls=(ToolCall("c1", "edit", {"p": "x"}),), reasoning="PLAN")


def _data_payloads(events: list[str]) -> list[dict]:
    out = []
    for ev in events:
        for line in ev.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[len("data: "):]))
    return out


# --- openai SSE ------------------------------------------------------------


def test_openai_sse_sequence_has_role_plan_content_toolcalls_finish_done():
    events = assemble_sse(_RESP, expose_plan_as="thinking", wire_format="openai")
    assert events[-1] == "data: [DONE]\n\n"
    payloads = _data_payloads(events)
    # first delta carries the role
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    # plan surfaced as reasoning_content (thinking)
    assert any(p["choices"][0]["delta"].get("reasoning_content") == "PLAN" for p in payloads)
    # content + tool_calls present; final chunk has finish_reason tool_calls
    assert any(p["choices"][0]["delta"].get("content") == "answer" for p in payloads)
    assert any("tool_calls" in p["choices"][0]["delta"] for p in payloads)
    assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_openai_sse_drop_omits_plan():
    events = assemble_sse(_RESP, expose_plan_as="drop", wire_format="openai")
    assert "PLAN" not in "".join(events)


def test_openai_sse_chunks_carry_full_envelope():
    """082 FR-013 (SSE sibling): every chat.completion.chunk MUST carry the
    id/object/created/model envelope, echoing upstream raw when present, so a
    strict streaming client accepts the synthesized stream."""
    raw = {"id": "chatcmpl-up", "created": 111, "model": "local/qwen3.6:35b"}
    resp = LLMResponse(content="answer", reasoning="PLAN", raw=raw)
    payloads = _data_payloads(assemble_sse(resp, expose_plan_as="drop", wire_format="openai"))
    assert payloads, "expected at least one chunk"
    for p in payloads:
        assert p["object"] == "chat.completion.chunk"
        assert p["id"] == "chatcmpl-up"
        assert p["created"] == 111
        assert p["model"] == "local/qwen3.6:35b"


# --- anthropic SSE ---------------------------------------------------------


def test_anthropic_sse_event_order_and_blocks():
    events = assemble_sse(_RESP, expose_plan_as="thinking", wire_format="anthropic")
    kinds = [ln[len("event: "):] for ev in events for ln in ev.splitlines() if ln.startswith("event: ")]
    assert kinds[0] == "message_start"
    assert kinds[-1] == "message_stop"
    assert "content_block_start" in kinds and "message_delta" in kinds
    payloads = _data_payloads(events)
    # a thinking block delta carries the plan
    assert any(p.get("delta", {}).get("type") == "thinking_delta"
               and p["delta"].get("thinking") == "PLAN" for p in payloads)
    # stop_reason reflects the tool call
    assert any(p.get("type") == "message_delta" and p["delta"].get("stop_reason") == "tool_use"
               for p in payloads)


# --- end-to-end streaming over the aiohttp shell ---------------------------


@pytest.mark.asyncio
async def test_proxy_streams_sse_when_requested():
    def handler(req: httpx.Request) -> httpx.Response:
        if '"tools"' not in req.content.decode():
            return httpx.Response(200, json={"choices": [{"message": {"content": "THE PLAN"}}]})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "done", "tool_calls": [{"id": "c1", "function": {"name": "edit", "arguments": "{}"}}]}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    orch = {
        "strategy": "always",
        "tool": {"name": "t", "model": "qwen", "wire_format": "openai", "base_url": "http://local/v1"},
        "thinking": {"name": "th", "model": "gptoss", "wire_format": "openai", "base_url": "http://local/v1"},
        "expose_plan_as": "thinking",
    }
    proxy = DualModelProxy(orchestration=orch, expose_plan_as="thinking", client=client)
    base = await proxy.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{base}/v1/chat/completions",
                json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "go"}],
                      "tools": [{"type": "function", "function": {"name": "edit", "parameters": {}}}]},
            )
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            text = r.text
            assert text.rstrip().endswith("data: [DONE]")
            assert "reasoning_content" in text and "THE PLAN" in text
            assert "edit" in text
    finally:
        await proxy.stop()
        await client.aclose()


def _chunk(delta: dict, finish: str | None = None, raw: dict | None = None) -> bytes:
    """One upstream chat.completion.chunk as an SSE frame."""
    payload = {
        "id": "chatcmpl-up", "object": "chat.completion.chunk",
        "created": 1, "model": "qwen",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


class _GateBlockedUpstream:
    """An act upstream that yields two chunks, then blocks on ``gate`` until the
    test has *received* the proxy's prefix blocks — proving incremental delivery."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.flushed = False

    async def stream(self) -> AsyncIterator[bytes]:
        yield _chunk({"role": "assistant"})
        yield _chunk({"content": "act one"})
        await self.gate.wait()
        self.flushed = True
        yield _chunk({"content": " act two"}, finish="stop")
        yield b"data: [DONE]\n\n"


def _proxy_orchestration() -> dict:
    return {
        "strategy": "always",
        "tool": {"name": "t", "model": "qwen", "wire_format": "openai", "base_url": "http://local/v1"},
        "thinking": {"name": "th", "model": "gptoss", "wire_format": "openai", "base_url": "http://local/v1"},
        "expose_plan_as": "thinking",
    }


_REQUEST_BODY = {
    "model": "x", "stream": True,
    "messages": [{"role": "user", "content": "go"}],
    "tools": [{"type": "function", "function": {"name": "edit", "parameters": {}}}],
}


def _block_payloads(block: str) -> list[dict]:
    out = []
    for line in block.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[len("data: "):]))
    return out


@pytest.mark.asyncio
async def test_proxy_streams_act_incremenally_before_upstream_completes():
    """FR-014 (true streaming): the first SSE block reaches the CLI while the
    act upstream is still generating. The upstream blocks on a gate until the
    client has received the role + plan prefix; a buffered implementation (run
    the strategy to completion, then write in one burst) times out instead."""
    upstream = _GateBlockedUpstream()

    def handler(req: httpx.Request) -> httpx.Response:
        if '"tools"' not in req.content.decode():
            return httpx.Response(200, json={"choices": [{"message": {"content": "THE PLAN"}}]})
        return httpx.Response(200, content=upstream.stream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = DualModelProxy(
        orchestration=_proxy_orchestration(), expose_plan_as="thinking", client=client,
    )
    base = await proxy.start()
    try:
        async with httpx.AsyncClient() as c:
            stream_cm = c.stream("POST", f"{base}/v1/chat/completions", json=_REQUEST_BODY)
            resp: httpx.Response | None = None
            try:
                try:
                    resp = await asyncio.wait_for(stream_cm.__aenter__(), 3.0)
                except asyncio.TimeoutError:
                    pytest.fail(
                        "response headers did not arrive while the act upstream was still "
                        "generating — the proxy buffers the whole turn (FR-014)"
                    )

                buf = b""
                aiter = resp.aiter_bytes()

                async def next_block() -> str:
                    nonlocal buf
                    while b"\n\n" not in buf:
                        chunk = await asyncio.wait_for(aiter.__anext__(), 3.0)
                        buf += chunk
                    cut = buf.index(b"\n\n") + 2
                    block, buf = buf[:cut], buf[cut:]
                    return block.decode()

                first = await next_block()
                [role_payload] = _block_payloads(first)
                assert role_payload["choices"][0]["delta"] == {"role": "assistant"}
                assert not upstream.flushed, "prefix written only after the upstream finished"

                second = await next_block()
                [plan_payload] = _block_payloads(second)
                assert plan_payload["choices"][0]["delta"]["reasoning_content"] == "THE PLAN"
                assert not upstream.flushed, "prefix written only after the upstream finished"

                upstream.gate.set()
                seen = []
                while True:
                    block = await next_block()
                    if block.strip() == "data: [DONE]":
                        break
                    seen.append(block)
                assert upstream.flushed
                texts = [
                    p["choices"][0]["delta"].get("content")
                    for b in seen for p in _block_payloads(b)
                ]
                assert [t for t in texts if t] == ["act one", " act two"]
            finally:
                upstream.gate.set()
                if resp is not None:
                    await stream_cm.__aexit__(None, None, None)
    finally:
        await proxy.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_streamed_blocks_match_buffered_assembly():
    """FR-014 streaming parity: with a single text fragment the streamed data
    payloads are identical to the buffered ``assemble_sse`` rendering."""
    def handler(req: httpx.Request) -> httpx.Response:
        if '"tools"' not in req.content.decode():
            return httpx.Response(200, json={"choices": [{"message": {"content": "THE PLAN"}}]})
        return httpx.Response(200, content=b"".join([
            _chunk({"role": "assistant"}),
            _chunk({"content": "done"}),
            _chunk({"tool_calls": [{"index": 0, "id": "c1",
                                    "function": {"name": "edit", "arguments": "{}"}}]}),
            _chunk({}, finish="tool_calls"),
            b"data: [DONE]\n\n",
        ]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = DualModelProxy(
        orchestration=_proxy_orchestration(), expose_plan_as="thinking", client=client,
    )
    base = await proxy.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/v1/chat/completions", json=_REQUEST_BODY)
            assert r.status_code == 200
            blocks = [b + "\n\n" for b in r.text.split("\n\n") if b.strip()]
    finally:
        await proxy.stop()
        await client.aclose()
    streamed = _data_payloads(blocks)
    buffered = _data_payloads(assemble_sse(
        LLMResponse(
            content="done", tool_calls=(ToolCall("c1", "edit", {}),), reasoning="THE PLAN",
            raw={"id": "chatcmpl-up", "created": 1, "model": "qwen"},
        ),
        expose_plan_as="thinking", wire_format="openai",
    ))
    assert streamed == buffered


@pytest.mark.asyncio
async def test_proxy_streams_responses_front_door():
    """FR-014: the responses front door streams sequence-numbered events —
    response.created first, output_text deltas, response.completed last."""

    def handler(req: httpx.Request) -> httpx.Response:
        if '"tools"' not in req.content.decode():
            return httpx.Response(200, json={"choices": [{"message": {"content": "THE PLAN"}}]})
        return httpx.Response(200, content=b"".join([
            _chunk({"role": "assistant"}),
            _chunk({"content": "act one"}),
            _chunk({"content": " act two"}, finish="stop"),
            b"data: [DONE]\n\n",
        ]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = DualModelProxy(
        orchestration=_proxy_orchestration(), expose_plan_as="thinking", client=client,
    )
    base = await proxy.start()
    body = {"model": "x", "stream": True, "input": "go",
            "tools": [{"type": "function", "name": "edit", "description": "", "parameters": {}}]}
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/v1/responses", json=body)
            assert r.status_code == 200
            types = [
                json.loads(ln[len("data: "):])["type"]
                for ln in r.text.splitlines()
                if ln.startswith("data: ") and ln != "data: [DONE]"
            ]
            assert types[0] == "response.created"
            assert types[-1] == "response.completed"
            assert types.count("response.output_text.delta") == 2
            assert "THE PLAN" in r.text and "act one" in r.text
            seqs = [
                json.loads(ln[len("data: "):])["sequence_number"]
                for ln in r.text.splitlines()
                if ln.startswith("data: ") and ln != "data: [DONE]"
            ]
            assert seqs == sorted(seqs)
    finally:
        await proxy.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_proxy_streams_anthropic_front_door():
    """FR-014: the anthropic front door streams message_start → thinking block →
    text deltas → message_delta/message_stop, driven by upstream fragments."""

    def handler(req: httpx.Request) -> httpx.Response:
        if '"tools"' not in req.content.decode():
            return httpx.Response(200, json={"choices": [{"message": {"content": "THE PLAN"}}]})
        return httpx.Response(200, content=b"".join([
            _chunk({"role": "assistant"}),
            _chunk({"content": "act one"}),
            _chunk({"content": " act two"}, finish="stop"),
            b"data: [DONE]\n\n",
        ]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = DualModelProxy(
        orchestration=_proxy_orchestration(), expose_plan_as="thinking", client=client,
    )
    base = await proxy.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/v1/messages", json=_REQUEST_BODY)
            assert r.status_code == 200
            kinds = [
                ln[len("event: "):] for ln in r.text.splitlines() if ln.startswith("event: ")
            ]
            assert kinds[0] == "message_start"
            assert kinds[-1] == "message_stop"
            assert kinds.count("content_block_delta") == 3  # thinking + two text fragments
            assert "THE PLAN" in r.text
            assert "act one" in r.text and " act two" in r.text
            assert any("stop_reason" in ln for ln in r.text.splitlines())
    finally:
        await proxy.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_proxy_stream_failure_before_first_block_returns_502():
    """FR-014 fail-safe: a strategy failure before the first block yields a clean
    502 JSON error, not a truncated stream."""
    def handler(req: httpx.Request) -> httpx.Response:
        if '"tools"' not in req.content.decode():
            return httpx.Response(200, json={"choices": [{"message": {"content": "THE PLAN"}}]})
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = DualModelProxy(
        orchestration=_proxy_orchestration(), expose_plan_as="thinking", client=client,
    )
    base = await proxy.start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/v1/chat/completions", json=_REQUEST_BODY)
            assert r.status_code == 502
            assert r.json()["error"]["message"] == "proxy orchestration failed"
    finally:
        await proxy.stop()
        await client.aclose()

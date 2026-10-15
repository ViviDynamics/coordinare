"""223: a delta that carries reasoning and content together keeps its content.

LiteLLM's ``/v1/messages`` adapter dropped the head of an answer whose first
``content`` shared a chunk with the end of ``reasoning`` (vLLM, glm-5.3-flash).
The performer's translate route reads ``delta.content`` and ignores reasoning,
so the same chunk sequence must keep every character, and the Anthropic event
sequence it emits must be well formed for the Claude CLI: one block open at a
time, indices contiguous from 0, every block started and stopped once.

Both reasoning field names are covered: vLLM and LiteLLM do not agree on
``reasoning`` versus ``reasoning_content``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from performer.proxy.normalizers import NORMALIZER_REGISTRY
from performer.proxy.normalizers.reasoning import StripReasoningNormalizer
from performer.proxy.routing import TargetDescriptor
from performer.proxy.shim import SelfHostedShim
from performer.proxy.translate.sse import TranslatingSSEFilter

MODEL = "glm-5.3-flash"
REASONING_FIELDS = ("reasoning", "reasoning_content")
DONE = b"data: [DONE]\n\n"


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-223",
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def _drive(chunks: list[bytes]) -> str:
    translator = TranslatingSSEFilter()
    out = bytearray()
    for chunk in chunks:
        out += translator.feed(chunk)
    out += translator.flush()
    return out.decode("utf-8")


def _events(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in raw.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.splitlines()
        name = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((name, data))
    return events


def _assert_well_formed(events: list[tuple[str, dict]]) -> None:
    """Anthropic block protocol: one open block at a time, contiguous indices."""
    names = [name for name, _ in events]
    assert names[0] == "message_start"
    assert names[-2:] == ["message_delta", "message_stop"]
    open_index: int | None = None
    started: list[int] = []
    for name, data in events:
        if name == "content_block_start":
            assert open_index is None, "a block opened while another was still open"
            open_index = data["index"]
            started.append(open_index)
        elif name == "content_block_delta":
            assert open_index == data["index"], "delta for a block that is not open"
        elif name == "content_block_stop":
            assert open_index == data["index"], "stop for a block that is not open"
            open_index = None
    assert open_index is None
    assert started == list(range(len(started))), "block indices must be 0..n-1 in order"


def _text_of(events: list[tuple[str, dict]], index: int) -> str:
    return "".join(
        data["delta"]["text"]
        for name, data in events
        if name == "content_block_delta"
        and data["index"] == index
        and data["delta"]["type"] == "text_delta"
    )


def _text_block_indices(events: list[tuple[str, dict]]) -> list[int]:
    return [
        data["index"]
        for name, data in events
        if name == "content_block_start" and data["content_block"]["type"] == "text"
    ]


# --- translator: one case per content shape ---------------------------------- #


@pytest.mark.parametrize("field", REASONING_FIELDS)
def test_issue_sequence_keeps_content_from_the_mixed_delta(field: str) -> None:
    raw = _drive([
        _chunk({field: " no fences.", "content": '{"'}),
        _chunk({"content": 'sufficient": true'}),
        _chunk({}, finish="stop"),
        DONE,
    ])
    events = _events(raw)
    _assert_well_formed(events)
    indices = _text_block_indices(events)
    assert indices == [0]
    text = _text_of(events, 0)
    assert text.startswith('{"sufficient"')
    assert text == '{"sufficient": true'


@pytest.mark.parametrize("field", REASONING_FIELDS)
def test_reasoning_only_opens_no_text_block(field: str) -> None:
    raw = _drive([
        _chunk({field: "thinking about it"}),
        _chunk({field: " more"}),
        _chunk({}, finish="stop"),
        DONE,
    ])
    events = _events(raw)
    _assert_well_formed(events)
    assert _text_block_indices(events) == []
    assert [name for name, _ in events] == [
        "message_start", "message_delta", "message_stop",
    ]


@pytest.mark.parametrize("field", REASONING_FIELDS)
def test_content_only_streams_one_text_block(field: str) -> None:
    raw = _drive([
        _chunk({"content": '{"'}),
        _chunk({"content": 'sufficient": true}'}),
        _chunk({}, finish="stop"),
        DONE,
    ])
    events = _events(raw)
    _assert_well_formed(events)
    assert _text_block_indices(events) == [0]
    assert _text_of(events, 0) == '{"sufficient": true}'


@pytest.mark.parametrize("field", REASONING_FIELDS)
def test_reasoning_after_content_does_not_split_the_text_block(field: str) -> None:
    raw = _drive([
        _chunk({"content": '{"a'}),
        _chunk({field: "checking"}),
        _chunk({"content": '": 1}'}),
        _chunk({}, finish="stop"),
        DONE,
    ])
    events = _events(raw)
    _assert_well_formed(events)
    assert _text_block_indices(events) == [0]
    assert _text_of(events, 0) == '{"a": 1}'


@pytest.mark.parametrize("field", REASONING_FIELDS)
def test_tool_call_after_reasoning_is_a_well_formed_tool_block(field: str) -> None:
    raw = _drive([
        _chunk({field: "I should read the file"}),
        _chunk({
            "tool_calls": [{
                "index": 0,
                "id": "call_223",
                "function": {"name": "Read", "arguments": '{"file_path":'},
            }],
        }),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"README.md"}'}}]}),
        _chunk({}, finish="tool_calls"),
        DONE,
    ])
    events = _events(raw)
    _assert_well_formed(events)
    starts = [data for name, data in events if name == "content_block_start"]
    assert len(starts) == 1
    assert starts[0]["index"] == 0
    assert starts[0]["content_block"]["type"] == "tool_use"
    assert starts[0]["content_block"]["name"] == "Read"
    partial = "".join(
        data["delta"]["partial_json"]
        for name, data in events
        if name == "content_block_delta"
    )
    assert json.loads(partial) == {"file_path": "README.md"}


# --- full chain: shim + strip_reasoning + translator -------------------------- #


def _target() -> TargetDescriptor:
    return TargetDescriptor(
        base_url="http://upstream.test/v1",
        wire_format="openai",
        strategy="translate",
        upstream_model=MODEL,
        normalizers=["strip_control_chars", "strip_reasoning"],
        health_probe="completion",
    )


def _sse_body(chunks: list[bytes]) -> bytes:
    return b"".join(chunks) + DONE


async def _post_stream(upstream_body: bytes) -> str:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200, content=upstream_body, headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        shim = SelfHostedShim(
            target=_target(),
            normalizers=[NORMALIZER_REGISTRY[key] for key in _target().normalizers],
            client=upstream,
        )
        base = await shim.start()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    base + "/v1/messages",
                    json={
                        "model": MODEL,
                        "max_tokens": 128,
                        "stream": True,
                        "messages": [{"role": "user", "content": "Return JSON"}],
                    },
                )
        finally:
            await shim.stop()
    assert response.status_code == 200
    return response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("field", REASONING_FIELDS)
async def test_shim_chain_keeps_the_head_of_a_mixed_stream(field: str) -> None:
    body = _sse_body([
        _chunk({field: '"\n\nOutput'}),
        _chunk({field: " strictly"}),
        _chunk({field: " no fences.", "content": '{"'}),
        _chunk({"content": 'sufficient": true'}),
        _chunk({}, finish="stop"),
    ])
    events = _events(await _post_stream(body))
    _assert_well_formed(events)
    assert _text_of(events, _text_block_indices(events)[0]) == '{"sufficient": true'


@pytest.mark.asyncio
async def test_legacy_reasoning_content_only_stream_is_promoted_to_text() -> None:
    body = _sse_body([
        _chunk({"reasoning_content": "ready"}),
        _chunk({}, finish="stop"),
    ])
    events = _events(await _post_stream(body))
    _assert_well_formed(events)
    assert _text_of(events, _text_block_indices(events)[0]) == "ready"


@pytest.mark.asyncio
async def test_vllm_reasoning_only_stream_is_not_promoted() -> None:
    """A ``reasoning`` draft is stripped, never promoted into the answer (223 BLOCK)."""
    body = _sse_body([
        _chunk({"reasoning": 'I think answer is {"sufficient": true}'}),
        _chunk({}, finish="stop"),
    ])
    events = _events(await _post_stream(body))
    _assert_well_formed(events)
    assert _text_block_indices(events) == []
    assert "sufficient" not in json.dumps(events)


@pytest.mark.asyncio
async def test_reasoning_content_stream_truncated_at_length_is_not_promoted() -> None:
    body = _sse_body([
        _chunk({"reasoning_content": "half a draft"}),
        _chunk({}, finish="length"),
    ])
    events = _events(await _post_stream(body))
    _assert_well_formed(events)
    assert _text_block_indices(events) == []


@pytest.mark.asyncio
async def test_vllm_reasoning_json_reply_is_stripped_not_promoted() -> None:
    """A content-empty vLLM ``reasoning`` reply fails empty, as it did before 223."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "glm-223",
            "model": MODEL,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": 'I think answer is {"sufficient": true}',
                },
                "finish_reason": "stop",
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        shim = SelfHostedShim(
            target=_target(),
            normalizers=[NORMALIZER_REGISTRY[key] for key in _target().normalizers],
            client=upstream,
        )
        base = await shim.start()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    base + "/v1/messages",
                    json={
                        "model": MODEL,
                        "max_tokens": 128,
                        "messages": [{"role": "user", "content": "Return JSON"}],
                    },
                )
        finally:
            await shim.stop()
    assert response.json()["content"] == []


def test_normalize_json_keeps_content_and_drops_both_reasoning_keys() -> None:
    body = {"id": "x", "model": MODEL, "choices": [{
        "index": 0, "finish_reason": "stop",
        "message": {
            "role": "assistant",
            "content": '{"sufficient": true}',
            "reasoning": "draft one",
            "reasoning_content": "draft two",
        },
    }]}
    message = StripReasoningNormalizer().normalize_json(body)["choices"][0]["message"]
    assert message["content"] == '{"sufficient": true}'
    assert "reasoning" not in message
    assert "reasoning_content" not in message


def test_normalize_json_does_not_promote_reasoning_content_truncated_at_length() -> None:
    body = {"id": "x", "model": MODEL, "choices": [{
        "index": 0, "finish_reason": "length",
        "message": {"role": "assistant", "content": "", "reasoning_content": "half"},
    }]}
    message = StripReasoningNormalizer().normalize_json(body)["choices"][0]["message"]
    assert message["content"] == ""


@pytest.mark.asyncio
async def test_legacy_reasoning_content_json_reply_is_promoted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "glm-223",
            "model": MODEL,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "", "reasoning_content": "ready"},
                "finish_reason": "stop",
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        shim = SelfHostedShim(
            target=_target(),
            normalizers=[NORMALIZER_REGISTRY[key] for key in _target().normalizers],
            client=upstream,
        )
        base = await shim.start()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    base + "/v1/messages",
                    json={
                        "model": MODEL,
                        "max_tokens": 128,
                        "messages": [{"role": "user", "content": "Reply ready"}],
                    },
                )
        finally:
            await shim.stop()
    assert response.json()["content"] == [{"type": "text", "text": "ready"}]

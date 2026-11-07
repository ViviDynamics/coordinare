"""A started streamed reply that stops sending is retried after minutes; one still queued keeps its whole allowance.

Dettmore e2e-61 waited the full 3,600 s read timeout twice for a stream that had stopped. Dettmore e2e-63 showed the
other side: a request queued behind a long provider queue sends nothing until it starts, so cutting it off at the idle
limit only sent another copy into the same queue.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from coordinare.services.conducting import STREAM_IDLE_SECONDS, _collect_stream, _stream_lines


class FakeStream:
    def __init__(self, *events: tuple[float, str]) -> None:
        self.events = events

    async def aiter_lines(self):
        for delay, line in self.events:
            await asyncio.sleep(delay)
            yield line


CHUNK = 'data: {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}'


@pytest.mark.asyncio
async def test_a_slow_start_is_never_cut_off_by_the_idle_limit():
    reply = await _collect_stream(FakeStream((0.2, CHUNK), (0.0, "data: [DONE]")), idle=0.05)
    assert reply is not None and reply["choices"][0]["message"]["content"] == "hi"


@pytest.mark.asyncio
async def test_a_started_stream_that_goes_silent_is_a_read_timeout():
    with pytest.raises(httpx.ReadTimeout, match="after it started"):
        await _collect_stream(FakeStream((0.0, CHUNK), (0.2, "data: [DONE]")), idle=0.05)


@pytest.mark.asyncio
async def test_without_an_idle_limit_every_line_is_awaited():
    lines = [line async for line in _stream_lines(FakeStream((0.0, "a"), (0.05, "b")), None)]
    assert lines == ["a", "b"]


def test_the_idle_limit_is_ten_minutes():
    assert STREAM_IDLE_SECONDS == 600.0

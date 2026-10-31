"""A discovery call rides out a provider outage; other callers keep the transport's few quick retries.

Dettmore e2e-26 and e2e-28 to e2e-31 each held within seconds of a provider outage. Maintainer ruling 2026-10-31:
retry for up to about ten minutes, spending the attempt's clock and no repair budget.
"""
from __future__ import annotations

import httpx
import pytest

from coordinare.services import conducting
from coordinare.services.conducting import OpenAiApiBackend
from coordinare.services.model_output_cap import provider_outage_seconds


def backend_with(monkeypatch, statuses: list[int | type[Exception]], sleeps: list[float]) -> OpenAiApiBackend:
    """Replies in turn with each status (or raises each transport error); the clock advances by each sleep."""
    clock = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = statuses.pop(0) if statuses else 200
        if isinstance(outcome, type):
            raise outcome('dropped', request=request)
        if outcome == 200:
            return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]})
        return httpx.Response(outcome, json={'error': 'upstream'})

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(conducting.asyncio, 'sleep', sleep)
    monkeypatch.setattr(conducting.time, 'monotonic', lambda: clock[0])
    return OpenAiApiBackend(model='m', api_key='k', base_url='http://x/v1')


@pytest.mark.asyncio
async def test_without_an_outage_window_a_call_gives_up_after_three_attempts(monkeypatch):
    sleeps: list[float] = []
    reply = await backend_with(monkeypatch, [500, 500, 500, 500], sleeps)._post_chat([{'role': 'user', 'content': 'x'}], True)
    assert reply.error and sleeps == [0.5, 1.5]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [500, 503, 429, httpx.ReadError, httpx.RemoteProtocolError])
async def test_a_discovery_call_rides_out_an_outage(monkeypatch, failure):
    sleeps: list[float] = []
    backend = backend_with(monkeypatch, [failure] * 7, sleeps)
    token = provider_outage_seconds.set(600.0)
    try:
        reply = await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    finally:
        provider_outage_seconds.reset(token)
    assert reply.error is None and reply.text == '{}'
    assert sleeps == [0.5, 1.5, 15.0, 30.0, 60.0, 120.0, 120.0]


@pytest.mark.asyncio
async def test_an_outage_longer_than_the_window_still_ends_the_call(monkeypatch):
    sleeps: list[float] = []
    backend = backend_with(monkeypatch, [500] * 50, sleeps)
    token = provider_outage_seconds.set(600.0)
    try:
        reply = await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    finally:
        provider_outage_seconds.reset(token)
    assert reply.error
    assert sum(sleeps) == pytest.approx(600.0) and max(sleeps) == 120.0


@pytest.mark.asyncio
async def test_a_client_error_is_never_retried(monkeypatch):
    sleeps: list[float] = []
    backend = backend_with(monkeypatch, [400], sleeps)
    token = provider_outage_seconds.set(600.0)
    try:
        reply = await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    finally:
        provider_outage_seconds.reset(token)
    assert reply.error and sleeps == []


def test_discovery_calls_carry_the_ten_minute_window():
    from coordinare.services.environment_response_schema import PROVIDER_OUTAGE_SECONDS

    assert PROVIDER_OUTAGE_SECONDS == 600.0

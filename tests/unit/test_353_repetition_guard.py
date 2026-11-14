"""A reply that degenerates into exact repetition is stopped while it streams, not at the output cap.

On SampleApp the GLM route looped on four replies in v21 alone ("flux? no - just finish:" and "1 | 1 | 1"),
each running to the 32 K cap at about 11 tokens a second. Discovery calls now stream, and the transport
stops reading once a tail repeats exactly; the author gets the cut-off reply back with guidance.
"""
from __future__ import annotations

import json

import httpx
import pytest

from coordinare.services.conducting import OpenAiApiBackend
from coordinare.services.environment_admission import explain_rejection
from coordinare.services.environment_attempt import AttemptBudget, DiscoveryChatReply
from coordinare.services.model_output_cap import REPEAT_SPAN, degenerate, repetition_guard

CHECK = {'id': 'bundle-usable', 'argv': ['bash', '-c', 'bundle --version'], 'cwd': '{repo}', 'depends_on': []}


@pytest.mark.parametrize('loop', ['flux? no — just finish:只放证据。 ', ' | 1', '\n', 'ab'])
def test_an_exactly_repeating_tail_is_degenerate(loop):
    text = '{"assessment":{"findings":[' + loop * (REPEAT_SPAN // len(loop) + 2)
    assert degenerate(text)


WORDS = ['bundle', 'rspec', 'rubocop', 'helm', 'compose', 'psql', 'node', 'rails', 'cucumber', 'brakeman', 'jq', 'gh', 'rake']


def test_a_long_envelope_of_similar_records_is_not_degenerate():
    checks = [{**CHECK, 'id': f'c00-{WORDS[index % 13]}-{WORDS[index * 7 % 13]}',
               'argv': ['bash', '-c', f'{WORDS[index % 13]} --version && {WORDS[index * 5 % 13]} check']}
              for index in range(200)]
    text = json.dumps({'manifest': {'checks': checks}})
    assert len(text) > 8 * REPEAT_SPAN
    assert not any(degenerate(text[:end]) for end in range(REPEAT_SPAN, len(text), 97))


def test_a_loop_that_only_counts_upward_is_degenerate():
    assert degenerate('{"ids":[' + ''.join(f'"end-{index}" \t,  ' for index in range(2000)))


def test_short_text_is_never_degenerate():
    assert not degenerate('x' * REPEAT_SPAN)


def sse(*chunks: dict) -> list[bytes]:
    return [f'data: {json.dumps(chunk)}\n\n'.encode() for chunk in chunks]


def delta(content: str = '', reasoning: str | None = None, finish: str | None = None) -> dict:
    body = {'content': content, **({'reasoning_content': reasoning} if reasoning is not None else {})}
    return {'model': 'm', 'choices': [{'delta': body, 'finish_reason': finish}]}


def backend_with(monkeypatch, frames: list[bytes], pulled: list[int], sent: list[dict]) -> OpenAiApiBackend:
    async def body():
        for frame in frames:
            pulled.append(1)
            yield frame

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=body())

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    return OpenAiApiBackend(model='m', api_key='k', base_url='http://x/v1', max_tokens=32768)


@pytest.mark.asyncio
async def test_a_looping_reply_is_stopped_early_and_reported_as_repetition(monkeypatch):
    frames = sse(delta('{"assessment":{"findings":[')) + sse(*[delta(' | 1')] * 5000) + sse(delta('', finish='length'))
    pulled: list[int] = []
    sent: list[dict] = []
    backend = backend_with(monkeypatch, frames, pulled, sent)
    token = repetition_guard.set(True)
    try:
        reply = await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    finally:
        repetition_guard.reset(token)
    assert reply.finish_reason == 'repetition'
    assert reply.text.startswith('{"assessment"')
    assert len(pulled) < 1000
    assert sent[0]['stream'] is True


@pytest.mark.asyncio
async def test_looping_hidden_reasoning_is_stopped_too(monkeypatch):
    frames = sse(*[delta(reasoning='no — just finish. ')] * 3000) + sse(delta('{}', finish='stop'))
    pulled: list[int] = []
    backend = backend_with(monkeypatch, frames, pulled, [])
    token = repetition_guard.set(True)
    try:
        reply = await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    finally:
        repetition_guard.reset(token)
    assert reply.finish_reason == 'repetition'
    assert len(pulled) < 1000


@pytest.mark.asyncio
async def test_a_streamed_reply_is_reassembled_with_its_usage(monkeypatch):
    usage = {'model': 'm', 'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 4}}
    frames = [*sse(delta('{"a":'), delta('1}', finish='stop'), usage), b'data: [DONE]\n\n']
    backend = backend_with(monkeypatch, frames, [], [])
    token = repetition_guard.set(True)
    try:
        reply = await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    finally:
        repetition_guard.reset(token)
    assert (reply.text, reply.finish_reason) == ('{"a":1}', 'stop')
    assert reply.usage == {'input_tokens': 10, 'output_tokens': 4, 'cache_read': 0, 'cache_write': 0}


@pytest.mark.asyncio
async def test_without_the_guard_a_call_is_not_streamed(monkeypatch):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    reply = await OpenAiApiBackend(model='m', api_key='k', base_url='http://x/v1')._post_chat(
        [{'role': 'user', 'content': 'x'}], True)
    assert reply.text == '{}'
    assert 'stream' not in sent[0]


def test_a_stopped_reply_is_corrected_with_repetition_guidance_and_counted():
    budget = AttemptBudget(max_calls=8, max_reads=8)
    budget.record_reply(DiscoveryChatReply(text='{"a":', finish_reason='repetition'), phase='review', seconds=1.0)
    assert budget.call_stats['review']['repetition_stops'] == 1
    assert budget.finish_reasons == {'repetition': 1}
    messages = [{'role': 'system', 'content': ''}, {'role': 'user', 'content': '{}'}]
    explain_rejection(messages, '{"a":', [{'code': 'invalid_json_envelope', 'location': []}], budget)
    assert 'reply_repetition' in messages[-1]['content']
    assert '1 | 1' not in messages[-1]['content']


@pytest.mark.asyncio
async def test_a_stream_the_provider_closes_mid_loop_is_still_a_repetition_stop(monkeypatch):
    """v33: a reply of blank lines ended without a finish reason before the guard's next check."""
    # The first frame is checked and not yet degenerate; the last adds too little to be checked inside the stream.
    frames = sse(delta('{"shard_checks":{"checks":[' + '\n \n' * 620), delta('\n \n' * 70))
    backend = backend_with(monkeypatch, frames, [], [])
    token = repetition_guard.set(True)
    try:
        reply = await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    finally:
        repetition_guard.reset(token)
    assert reply.finish_reason == 'repetition'

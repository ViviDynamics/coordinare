"""Per-call telemetry: where an attempt's time and tokens go, by phase (throughput amendment, section 1)."""
from __future__ import annotations

import pytest

from coordinare.services.environment_attempt import AttemptBudget, DiscoveryChatReply
from coordinare.services.environment_response_schema import invoke_recorded


def budget() -> AttemptBudget:
    return AttemptBudget(max_calls=16, max_reads=16)


def test_calls_are_aggregated_per_phase_from_reported_usage():
    attempt = budget()
    attempt.record_reply(DiscoveryChatReply('{}', usage={'input_tokens': 100, 'output_tokens': 40, 'cache_read': 60},
                                            finish_reason='stop'), phase='listing_triage', seconds=2.5)
    attempt.record_reply(DiscoveryChatReply('{}', usage={'input': 10, 'output': 32768}, finish_reason='length'),
                         phase='listing_triage', seconds=3000.0)
    attempt.record_reply('{}', phase='discovery', seconds=1.0)
    stats = attempt.summary()['call_stats']
    assert stats['listing_triage'] == {'calls': 2, 'seconds': 3002.5, 'input_tokens': 110, 'output_tokens': 32808,
                                       'cached_tokens': 60, 'length_stops': 1, 'repetition_stops': 0}
    assert stats['discovery']['calls'] == 1 and stats['discovery']['input_tokens'] == 0


def test_untrusted_usage_values_are_ignored():
    attempt = budget()
    attempt.record_reply(DiscoveryChatReply('{}', usage={'input_tokens': -5, 'output_tokens': 10**13, 'cache_read': 3}),
                         phase='review', seconds=-1.0)
    assert attempt.summary()['call_stats']['review'] == {'calls': 1, 'seconds': 0.0, 'input_tokens': 0,
                                                         'output_tokens': 0, 'cached_tokens': 3, 'length_stops': 0, 'repetition_stops': 0}


def test_a_call_without_a_phase_is_counted_but_not_attributed():
    attempt = budget()
    attempt.record_reply('{}')
    assert attempt.calls_completed == 1
    assert attempt.summary()['call_stats'] == {}


@pytest.mark.asyncio
async def test_every_model_call_is_timed_against_its_phase():
    attempt = budget()

    async def chat(messages, **_kwargs):
        return DiscoveryChatReply('{"ok":true}', usage={'input_tokens': 7, 'output_tokens': 3}, finish_reason='stop')

    text = await invoke_recorded(attempt, chat, [{'role': 'user', 'content': 'x'}], schema={}, max_context_bytes=None,
                                 context_phase='shard_summary')
    assert text == '{"ok":true}'
    stats = attempt.summary()['call_stats']['shard_summary']
    assert stats['calls'] == 1 and stats['input_tokens'] == 7 and stats['seconds'] >= 0


@pytest.mark.asyncio
async def test_a_phase_cap_bounds_only_its_own_call():
    from coordinare.services.model_output_cap import capped, output_token_cap

    seen: list[int] = []

    async def chat(messages, **_kwargs):
        seen.append(capped(32768))
        return '{}'

    attempt = budget()
    await invoke_recorded(attempt, chat, [], schema={}, max_context_bytes=None, context_phase='scope_triage')
    await invoke_recorded(attempt, chat, [], schema={}, max_context_bytes=None, context_phase='discovery')
    assert seen == [16384, 32768]
    assert output_token_cap.get() is None
    assert capped(8192) == 8192


@pytest.mark.asyncio
async def test_the_openai_transport_sends_the_capped_limit(monkeypatch):
    import httpx

    from coordinare.services.conducting import OpenAiApiBackend
    from coordinare.services.model_output_cap import output_token_cap

    sent: list[dict] = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}], 'usage': {}}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None):
            sent.append(json)
            return Response()

    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    backend = OpenAiApiBackend(model='m', api_key='k', base_url='http://x/v1', max_tokens=32768)
    token = output_token_cap.set(16384)
    try:
        await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    finally:
        output_token_cap.reset(token)
    await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    assert [payload['max_tokens'] for payload in sent] == [16384, 32768]


def test_a_cut_off_reply_is_corrected_with_length_guidance():
    from coordinare.services.environment_admission import explain_rejection

    attempt = budget()
    attempt.record_reply(DiscoveryChatReply('{"listing_triage":', finish_reason='length'), phase='scope_triage', seconds=1)
    messages = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '{}'}]
    explain_rejection(messages, '{"listing_triage":', [{'code': 'invalid_json_envelope', 'location': []}], attempt)
    assert 'reply_length_limit' in messages[-1]['content']
    attempt.record_reply(DiscoveryChatReply('{}', finish_reason='stop'), phase='scope_triage', seconds=1)
    explain_rejection(messages, '{}', [{'code': 'missing', 'location': []}], attempt)
    assert 'reply_length_limit' not in messages[-1]['content']

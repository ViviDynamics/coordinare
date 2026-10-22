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
                                       'cached_tokens': 60, 'length_stops': 1}
    assert stats['discovery']['calls'] == 1 and stats['discovery']['input_tokens'] == 0


def test_untrusted_usage_values_are_ignored():
    attempt = budget()
    attempt.record_reply(DiscoveryChatReply('{}', usage={'input_tokens': -5, 'output_tokens': 10**13, 'cache_read': 3}),
                         phase='review', seconds=-1.0)
    assert attempt.summary()['call_stats']['review'] == {'calls': 1, 'seconds': 0.0, 'input_tokens': 0,
                                                         'output_tokens': 0, 'cached_tokens': 3, 'length_stops': 0}


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

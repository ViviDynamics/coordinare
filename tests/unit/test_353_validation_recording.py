"""Validation evidence survives overlapping calls and divided review prompts."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from scripts import validate_environment_repositories as harness

from coordinare.services.env_cache import EnvCacheService
from coordinare.services.environment_attempt import EnvironmentDiscoveryError


async def record_fixture(tmp_path, monkeypatch, backend, exercise):
    async def verify():
        return None

    async def build(self, **kwargs):
        await exercise(kwargs['llm_chat'])
        raise EnvironmentDiscoveryError('Local recording fixture completed', code='fixture_complete')

    events = []
    monkeypatch.setattr(EnvCacheService, '_build_manifest_artifacts', build)
    monkeypatch.setattr(EnvCacheService, 'prepare_bootstrap_generation', lambda *_args: None)
    monkeypatch.setattr(harness, '_event', lambda **event: events.append(event))
    output = tmp_path / 'profile'
    result = await harness.validate_repository(
        SimpleNamespace(name='suite', org='fixture', commit='a' * 40, verify=verify),
        image='trusted@sha256:' + 'b' * 64, output=output,
        profile=harness.Profile(model='fixture', base_url='http://localhost:1', save_transcripts=True), chat=backend)
    return result, output, events


@pytest.mark.asyncio
async def test_concurrent_calls_keep_distinct_transcript_and_event_ids(tmp_path, monkeypatch):
    second_finished = asyncio.Event()

    async def backend(messages):
        name = json.loads(messages[1]['content'])['id']
        if name == 'first':
            await second_finished.wait()
        else:
            second_finished.set()
        return json.dumps({'response_for': name})

    async def exercise(recorded):
        async with asyncio.timeout(5):
            await asyncio.gather(*(recorded([
                {'role': 'system', 'content': 'fixture'},
                {'role': 'user', 'content': json.dumps({'id': name})}], context_phase='discovery')
                for name in ('first', 'second')))

    result, output, events = await record_fixture(tmp_path, monkeypatch, backend, exercise)
    assert result['model_calls'] == 2
    transcripts = sorted(output.glob('chat-*.json'))
    assert [p.name for p in transcripts] == ['chat-001.json', 'chat-002.json']
    for number, (path, name) in enumerate(zip(transcripts, ('first', 'second'), strict=True), 1):
        saved = json.loads(path.read_text())
        assert saved['call'] == number and saved['phase'] == 'discovery'
        assert json.loads(saved['messages'][1]['content'])['id'] == name
        assert json.loads(saved['response']) == {'response_for': name}
    assert sorted(event['call'] for event in events if 'call' in event) == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize('phase, content, source_count, executable_count', [
    ('review', '{"shared":{},"own":{}}', 0, 1),
    ('review', 'Opaque review content', 0, 1),
    ('scope_review', '{}', 1, 0),
    ('scope_shard_review', '{}', 1, 0),
    ('scope_merge_review', '{}', 1, 0),
    ('discovery', '{"manifest":{}}', 0, 0),
])
async def test_review_counters_use_trusted_phase_and_count_conversations_once(
    tmp_path, monkeypatch, phase, content, source_count, executable_count,
):
    async def backend(_messages):
        return '{}'

    async def exercise(recorded):
        messages = [{'role': 'system', 'content': 'fixture'}, {'role': 'user', 'content': content}]
        await recorded(messages, context_phase=phase)
        await recorded([*messages, {'role': 'assistant', 'content': '{}'}], context_phase=phase)

    result, _output, _events = await record_fixture(tmp_path, monkeypatch, backend, exercise)
    assert result['reason_code'] == 'fixture_complete'
    assert result['model_calls'] == 2
    assert result['independent_source_review_invocations'] == source_count
    assert result['independent_executable_review_invocations'] == executable_count
    assert result['independent_source_review_reached'] is bool(source_count)
    assert result['independent_review_reached'] is bool(executable_count)
    assert result['ready_at_validation'] is False

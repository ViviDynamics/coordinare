"""Cached prompt tokens are read from every usage shape the gateway can send, and the stable prompt prefix comes first.

The v40 ledger recorded cache_read 0 on every call. The live gateway reports no cached-token field at all for the
self-hosted route (one identical 6.6 K-token prompt sent twice gave the same usage), so the parser is not what
dropped it; the parser must still read the cached field when a route reports one, streamed or not.
Prompt order is pinned so that per-call material follows the byte-identical prefix that a prefix cache can reuse.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from coordinare.services.conducting import OpenAiApiBackend, _openai_usage
from coordinare.services.environment_discovery import review_part_messages
from coordinare.services.environment_discovery_prompts import (
    _MANIFEST_INSTRUCTIONS,
    SOURCE_TOOLS,
    DiscoveryPrompts,
)
from coordinare.services.environment_shard_scope import agent_context
from coordinare.services.model_output_cap import repetition_guard


def test_cached_prompt_tokens_come_from_prompt_details():
    usage = {'prompt_tokens': 100, 'completion_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 60}}
    assert _openai_usage({'usage': usage}) == {'input_tokens': 40, 'output_tokens': 5, 'cache_read': 60, 'cache_write': 0}


def test_cached_prompt_tokens_fall_back_to_the_top_level_anthropic_field():
    usage = {'prompt_tokens': 100, 'completion_tokens': 5, 'cache_read_input_tokens': 60}
    assert _openai_usage({'usage': usage}) == {'input_tokens': 40, 'output_tokens': 5, 'cache_read': 60, 'cache_write': 0}


def test_absent_cache_fields_read_as_zero_without_inventing_input():
    assert _openai_usage({'usage': {'prompt_tokens': 100, 'completion_tokens': 5}}) == {
        'input_tokens': 100, 'output_tokens': 5, 'cache_read': 0, 'cache_write': 0}


def sse(*chunks: dict) -> list[bytes]:
    return [f'data: {json.dumps(chunk)}\n\n'.encode() for chunk in chunks]


def delta(content: str = '', finish: str | None = None) -> dict:
    return {'model': 'm', 'choices': [{'delta': {'content': content}, 'finish_reason': finish}]}


def backend_with(monkeypatch, frames: list[bytes]) -> OpenAiApiBackend:
    async def body():
        for frame in frames:
            yield frame

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    return OpenAiApiBackend(model='m', api_key='k', base_url='http://x/v1', max_tokens=32768)


@pytest.mark.asyncio
async def test_streamed_cached_tokens_on_the_final_usage_chunk_are_kept(monkeypatch):
    # Some gateways send usage: null on content chunks and the real block after the finish chunk.
    usage_chunk = {'model': 'm', 'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 4,
                                                          'prompt_tokens_details': {'cached_tokens': 60}}}
    frames = [*sse(delta('{"a":'), {**delta('1}', finish='stop'), 'usage': None}, usage_chunk), b'data: [DONE]\n\n']
    backend = backend_with(monkeypatch, frames)
    token = repetition_guard.set(True)
    try:
        reply = await backend._post_chat([{'role': 'user', 'content': 'x'}], True)
    finally:
        repetition_guard.reset(token)
    assert (reply.text, reply.finish_reason) == ('{"a":1}', 'stop')
    assert reply.usage == {'input_tokens': 40, 'output_tokens': 4, 'cache_read': 60, 'cache_write': 0}


def _manifest_budget(known: list[str]) -> SimpleNamespace:
    records = [SimpleNamespace(id=identifier) for identifier in known]
    return SimpleNamespace(probe_reports=[object()], finding_ledger=SimpleNamespace(records=records), expected_envelope=None)


def test_manifest_round_prefix_is_identical_and_the_finding_list_comes_last():
    first, later = [{'role': 'system', 'content': ''}], [{'role': 'system', 'content': ''}]
    DiscoveryPrompts().update(first, _manifest_budget(['coverage-finding-alpha']), {'a.yml': 'd'})
    DiscoveryPrompts().update(later, _manifest_budget(['coverage-finding-alpha', 'coverage-finding-beta']), {'a.yml': 'd'})
    before, after = first[0]['content'], later[0]['content']
    stable = before.index('Return exactly the complete envelope matching this schema')
    assert before[:stable] == after[:stable]
    assert before.startswith(SOURCE_TOOLS + 'Current phase: manifest_construction. ' + _MANIFEST_INSTRUCTIONS)
    assert 'coverage-finding-alpha' not in before[:stable]
    assert 'coverage-finding-alpha' in before[stable:]
    assert 'coverage-finding-beta' in after[stable:]


def test_review_parts_share_a_byte_identical_prefix_in_one_user_message():
    shared = {'base': {'requested_scope': 'x'}, 'manifest_digest': 'd', 'global_part': {'checks': 3},
              'probe_evidence': [], 'untrusted_execution_history': []}
    one = review_part_messages('SYSTEM', shared, {'part': {'prefix': 'a'}, 'assigned_findings': [{'id': 'f1'}]})
    two = review_part_messages('SYSTEM', shared, {'part': {'prefix': 'b'}, 'assigned_findings': [{'id': 'f2'}]})
    assert [message['role'] for message in one] == ['system', 'user']
    assert one[0] == {'role': 'system', 'content': 'SYSTEM'}
    shared_text = _json_text(shared)
    assert one[1]['content'].startswith(shared_text) and two[1]['content'].startswith(shared_text)
    assert one[1]['content'][:len(shared_text)] == two[1]['content'][:len(shared_text)]
    assert '"part"' not in shared_text and '"assigned_findings"' not in shared_text
    assert one[1]['content'] != two[1]['content']
    assert '"assigned_findings":[{"id":"f1"}]' in one[1]['content']


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def test_shared_agent_context_is_serialized_before_the_agent_own_material():
    text = agent_context({'spine_evidence': ['s']}, {'shard': {'id': 'x'}})
    assert text.startswith('{"_shared_context":')
    assert text.index('"_shared_context"') < text.index('"shard"')


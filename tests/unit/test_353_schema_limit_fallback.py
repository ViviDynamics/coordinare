"""A provider schema-limit rejection resends the same request once without a compiled grammar."""
from __future__ import annotations

import json

import httpx
import pytest

from coordinare.services.conducting import OpenAiApiBackend
from coordinare.services.environment_attempt import AttemptBudget, DiscoveryChatReply
from coordinare.services.environment_response_schema import response_schema

GRAMMAR = ('{"error":{"message":"litellm.BadRequestError: AnthropicException - The compiled grammar is too large, '
           'which would cause performance issues."}}')
COMPLEX = '{"error":{"message":"litellm.BadRequestError: AnthropicException - Schema is too complex."}}'
READ = {'tool': 'read_file', 'path': 'Makefile'}


async def send(monkeypatch, answers, *, transport='json_schema', phase='manifest'):
    requests = []
    replies = iter(answers)

    def answer(request):
        requests.append(json.loads(request.content))
        status, body = next(replies)
        return httpx.Response(status, text=body if isinstance(body, str) else json.dumps(body))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(answer), **kw))
    backend = OpenAiApiBackend(api_key='fixture', structured_transport=transport)
    reply = await backend.chat_json_with_metadata([{'role': 'system', 'content': 'Pinned source only.'}],
                                                  response_schema=response_schema(phase))
    return reply, requests


def ok(content):
    return 200, {'choices': [{'message': {'content': json.dumps(content)}, 'finish_reason': 'stop'}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('rejection', [GRAMMAR, COMPLEX])
async def test_schema_limit_rejection_resends_once_as_json_object(monkeypatch, rejection):
    reply, requests = await send(monkeypatch, [(400, rejection), ok(READ)])
    assert len(requests) == 2
    assert requests[0]['response_format']['type'] == 'json_schema'
    assert requests[1]['response_format'] == {'type': 'json_object'}
    fallback_system = requests[1]['messages'][0]['content']
    assert 'Authoritative logical response schema for this request' in fallback_system
    assert 'EnvironmentManifestProposal' in fallback_system
    assert reply.error_code is None and json.loads(reply.text) == READ
    assert reply.transport_fallback == 'json_object'


@pytest.mark.asyncio
async def test_other_bad_request_does_not_fall_back(monkeypatch):
    reply, requests = await send(monkeypatch, [(400, '{"error":{"message":"invalid model"}}')])
    assert len(requests) == 1 and reply.error_code == 'provider_request_failed'
    assert reply.transport_fallback is None


@pytest.mark.asyncio
async def test_failed_fallback_still_fails_closed(monkeypatch):
    reply, requests = await send(monkeypatch, [(400, GRAMMAR), (400, '{"error":{"message":"invalid"}}')])
    assert len(requests) == 2 and reply.error_code == 'provider_request_failed' and reply.text == ''


@pytest.mark.asyncio
async def test_json_object_transport_never_needs_fallback(monkeypatch):
    reply, requests = await send(monkeypatch, [(400, GRAMMAR)], transport='json_object')
    assert len(requests) == 1 and reply.error_code == 'provider_request_failed'


@pytest.mark.asyncio
async def test_successful_strict_request_is_unchanged(monkeypatch):
    reply, requests = await send(monkeypatch, [ok({'response': READ})], phase='scope_assessment')
    assert len(requests) == 1 and reply.transport_fallback is None and json.loads(reply.text) == READ


def test_budget_summary_counts_transport_fallbacks():
    budget = AttemptBudget()
    budget.record_reply(DiscoveryChatReply(text='{}', transport_fallback='json_object'))
    budget.record_reply(DiscoveryChatReply(text='{}'))
    assert budget.summary()['transport_fallbacks'] == 1


def test_failed_fallback_is_still_counted():
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError):
        budget.record_reply(DiscoveryChatReply(text='', error_code='provider_request_failed', transport_fallback='json_object'))
    assert budget.summary()['transport_fallbacks'] == 1


@pytest.mark.asyncio
async def test_marker_on_non_400_status_does_not_fall_back(monkeypatch):
    reply, requests = await send(monkeypatch, [(422, GRAMMAR)])
    assert len(requests) == 1 and reply.transport_fallback is None and reply.error_code == 'provider_request_failed'

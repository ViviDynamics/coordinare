"""Native function transport preserves existing admission contracts and rejects ambiguity."""
from __future__ import annotations

import json

import httpx
import pytest

from coordinare.services.conducting import OpenAiApiBackend
from coordinare.services.environment_response_schema import response_schema


def message(name='read_files', arguments='{"paths":["Makefile"]}'):
    return {'content': None, 'tool_calls': [{'type': 'function', 'id': 'fixture',
            'function': {'name': name, 'arguments': arguments}}]}


async def send(monkeypatch, reply_message, phase='probe', status=200):
    requests = []
    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, json={'choices': [{'message': reply_message, 'finish_reason': 'tool_calls'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(answer), **kw))
    backend = OpenAiApiBackend(api_key='fixture', structured_transport='tool_call')
    reply = await backend.chat_json_with_metadata([{'role': 'system', 'content': 'Pinned source only.'}], response_schema=response_schema(phase))
    return reply, requests


@pytest.mark.asyncio
async def test_function_source_reply_preserves_canonical_contract_and_wire(monkeypatch):
    raw = message()
    reply, requests = await send(monkeypatch, raw)
    assert reply.error_code is None
    assert json.loads(reply.text) == {'tool': 'read_files', 'paths': ['Makefile']}
    assert json.loads(reply.wire_text) == raw
    request = requests[0]
    assert 'response_format' not in request
    assert request['tool_choice'] == 'required'
    assert request['parallel_tool_calls'] is False
    functions = {item['function']['name']: item['function'] for item in request['tools']}
    assert set(functions) == {'read_file', 'read_files', 'list_files', 'submit_environment_probe'}
    assert 'commands' in functions['submit_environment_probe']['parameters']['properties']
    assert 'tool' not in functions['read_files']['parameters']['properties']


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', [
    {'content': '{}'},
    {**message(), 'content': 'also accept this'},
    {**message(), 'tool_calls': message()['tool_calls'] * 2},
    message('unadvertised'),
    message(arguments='{"paths":[],"paths":["Makefile"]}'),
    message(arguments='{"paths":["Makefile"],"tool":"read_file"}'),
    message(arguments='[]'),
    {'content': None, 'tool_calls': [None]},
    {'content': None, 'tool_calls': [{'type': 'function', 'function': None}]},
    message(arguments='NaN'),
])
async def test_ambiguous_or_duplicate_function_reply_is_never_selected(monkeypatch, malformed):
    reply, _ = await send(monkeypatch, malformed)
    assert not reply.text
    assert reply.error_code == 'provider_response_invalid'


@pytest.mark.asyncio
async def test_manifest_function_flattens_only_documented_envelope(monkeypatch):
    from tests.unit.test_353_discovery_repair import candidate
    manifest = candidate()
    args = {**manifest, 'finding_responses': [], 'unexpected': 'must reach validation'}
    reply, requests = await send(monkeypatch, message('submit_environment_manifest', json.dumps(args)), 'manifest')
    assert json.loads(reply.text) == {'manifest': {**manifest, 'unexpected': 'must reach validation'}, 'finding_responses': []}
    function = requests[0]['tools'][-1]['function']
    assert 'install' in function['parameters']['properties']
    assert 'manifest' not in function['parameters']['properties']


@pytest.mark.asyncio
async def test_review_function_wraps_exact_assessment(monkeypatch):
    reply, _ = await send(monkeypatch, message('submit_environment_assessment', '{"verdict":"revise"}'), 'assessment')
    assert json.loads(reply.text) == {'assessment': {'verdict': 'revise'}}


@pytest.mark.asyncio
async def test_function_transport_rejection_never_falls_back(monkeypatch):
    reply, requests = await send(monkeypatch, message(), status=400)
    assert reply.error_code == 'provider_request_failed'
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_object_function_parameters_expose_types_without_reference_resolution(monkeypatch):
    _, requests = await send(monkeypatch, message('submit_environment_manifest', '{"finding_responses":[]}'), 'manifest')
    parameters = requests[0]['tools'][-1]['function']['parameters']
    assert parameters['properties']['activation']['type'] == 'object'
    assert parameters['properties']['checks']['items']['type'] == 'object'
    assert parameters['properties']['checks']['items']['properties']['env']['type'] == 'object'


@pytest.mark.asyncio
async def test_duplicate_provider_message_keys_are_rejected_before_json_collapses_them(monkeypatch):
    raw = '{"choices":[{"message":{"content":"ambiguous prose","content":null,"tool_calls":[],"tool_calls":[{"type":"function","function":{"name":"read_file","arguments":"{\\\"path\\\":\\\"Makefile\\\"}"}}]},"finish_reason":"tool_calls"}]}'
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=raw)), **kw))
    reply = await OpenAiApiBackend(api_key='fixture', structured_transport='tool_call').chat_json_with_metadata(
        [{'role': 'user', 'content': 'Read source'}], response_schema=response_schema('source_tool'))
    assert reply.error_code is not None
    assert not reply.text


@pytest.mark.asyncio
async def test_explicit_json_text_avoids_provider_grammar_and_preserves_logical_contract(monkeypatch):
    requests = []
    raw = '{"tool":"read_files","paths":["Makefile"]}'
    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content':raw}, 'finish_reason':'stop'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(answer), **kw))
    backend = OpenAiApiBackend(api_key='fixture', structured_transport='json_text')
    reply = await backend.chat_json_with_metadata([{'role':'system','content':'Logical phase schema.'}],response_schema=response_schema('source_tool'))
    assert reply.text == raw
    assert reply.wire_text == raw
    assert reply.error_code is None
    assert 'response_format' not in requests[0]
    assert 'tools' not in requests[0]
    assert requests[0]['messages'][0]['content'].startswith('Logical phase schema.')
    # Transport choice applies only to environment schema calls.
    await backend.chat_json_with_metadata([{'role':'user','content':'legacy'}])
    assert requests[1]['response_format'] == {'type':'json_object'}


@pytest.mark.asyncio
async def test_json_text_failure_never_retries_as_another_transport(monkeypatch):
    requests = []
    def answer(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={'error':'unsupported'})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(answer),**kw))
    reply=await OpenAiApiBackend(api_key='fixture',structured_transport='json_text').chat_json_with_metadata(
        [{'role':'user','content':'read evidence'}],response_schema=response_schema('source_tool'))
    assert reply.error_code == 'provider_request_failed'
    assert len(requests) == 1
    assert 'response_format' not in requests[0]

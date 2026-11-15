"""Configured answer encodings survive the native runtime and accounting seam."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import httpx2
import pytest
from nare import Reply, Usage

from coordinare.config import ConductingConfig
from coordinare.services.agent_runtime.accounting import (
    BudgetBridge,
    BudgetTransport,
    TransportContext,
)
from coordinare.services.agent_runtime.configuration import resolve_conducting
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_376_agent_runtime import ScriptedTransport, request

MODES = ('json_schema', 'json_object', 'tool_call')


async def save() -> None:
    pass


async def admit(answer: Any) -> list[Any]:
    return []


@pytest.mark.parametrize('mode', MODES)
def test_answer_encoding_survives_configuration_and_checkpoint_identity(mode: Any) -> None:
    model = resolve_conducting(ConductingConfig(backend='openai_api', structured_transport=mode),
                              environ={'OPENAI_API_KEY': 'fixture-key'})
    assert model.structured_transport == mode
    assert asdict(model)['structured_transport'] == mode


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', MODES)
async def test_configured_answer_encoding_reaches_provider_and_call_metadata(
    mode: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime
    from coordinare.services.conductor_calls import ConductorCallLog
    from coordinare.services.run_transcript import TranscriptCollector

    sent = []

    def provider(incoming: httpx2.Request) -> httpx2.Response:
        if incoming.method == 'GET':
            return httpx2.Response(200, json={'data': [{'model_name': 'fixture/native',
                'model_info': {'max_input_tokens': 262144}}]})
        body = json.loads(incoming.content)
        sent.append(body)
        message: dict[str, Any] = {'role': 'assistant', 'content': '{"accepted":true}'}
        finish = 'stop'
        if mode == 'tool_call' and body.get('tools'):
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'answer-1', 'type': 'function', 'function': {
                    'name': body['tools'][0]['function']['name'], 'arguments': '{"accepted":true}'}}]}
            finish = 'tool_calls'
        return httpx2.Response(200, json={'choices': [{'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7}})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(provider))
    monkeypatch.setattr(httpx2, 'AsyncClient', lambda **kwargs: client)
    config = resolve_conducting(ConductingConfig(backend='openai_api', model='fixture/native',
        base_url='https://model.example/v1', structured_transport=mode), environ={'OPENAI_API_KEY': 'fixture-key'})
    collector = TranscriptCollector(tmp_path, enabled=True, max_bytes_per_run=1000000, retention_days=0)
    calls = ConductorCallLog(collector)
    req = request(admit)
    try:
        result = await NareRuntime(config=config, calls=calls).run(req)
    finally:
        await client.aclose()
        await calls.drain()
    assert result.status == 'accepted' and result.answer == {'accepted': True}
    assert len(sent) == 1 and req.budget.calls == 1
    body = sent[0]
    if mode == 'json_object':
        assert body['response_format'] == {'type': 'json_object'}
        assert 'tools' not in body
    elif mode == 'json_schema':
        assert body['response_format']['type'] == 'json_schema'
        assert body['response_format']['json_schema']['schema'] == req.answer_schema
        assert 'tools' not in body
    else:
        assert 'response_format' not in body
        assert len(body['tools']) == 1
        function = body['tools'][0]['function']
        assert function['parameters'] == req.answer_schema
        assert body['tool_choice'] == {'type': 'function', 'function': {'name': function['name']}}
        assert body['parallel_tool_calls'] is False
    headers = [json.loads(path.read_text().splitlines()[0]) for path in tmp_path.rglob('*.jsonl')]
    assert len(headers) == 1
    assert headers[0]['conductor']['parameters']['structured_transport'] == mode


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('mode', ['json_object', 'tool_call'])
async def test_accounting_preserves_answer_encoding_on_both_transport_paths(mode: str, streaming: bool) -> None:
    class Encoded(ScriptedTransport):
        supports_structured_output = True
        structured_output_modes = MODES

        async def turn(self, messages: Any, tools: Any, *, response_schema: Any = None,
                       response_mode: Any = None) -> Reply:
            self.requests.append({'schema': response_schema, 'mode': response_mode, 'tools': tools})
            return Reply([{'type': 'text', 'text': '{"accepted":true}'}], [], Usage(input=11, output=7), 'end_turn')

        async def stream_turn(self, messages: Any, tools: Any, **kwargs: Any):
            yield await self.turn(messages, tools, **kwargs)

    inner = Encoded('')
    budget = AttemptBudget()
    transport = BudgetTransport(inner, BudgetBridge(budget, phase='review'),
        TransportContext(session_id='fixture', before_send=save))
    schema = request(admit).answer_schema
    if streaming:
        replies = [item async for item in transport.stream_turn([], [], response_schema=schema, response_mode=mode)]
    else:
        replies = [await transport.turn([], [], response_schema=schema, response_mode=mode)]
    assert inner.requests == [{'schema': schema, 'mode': mode, 'tools': []}]
    assert transport.structured_output_modes == MODES
    assert len(replies) == 1 and budget.calls == budget.calls_completed == 1


@pytest.mark.asyncio
async def test_unsupported_response_mode_signature_refuses_before_reserving_a_call() -> None:
    budget = AttemptBudget()
    inner = ScriptedTransport('{}')
    wrapped = BudgetTransport(inner, BudgetBridge(budget, phase='review'),
        TransportContext(session_id='fixture', before_send=save))
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await wrapped.turn([], [], response_schema={'type': 'object'}, response_mode='json_object')
    assert caught.value.code == 'agent_transport_unsupported'
    assert budget.calls == 0 and inner.requests == []


@pytest.mark.parametrize('legacy_support', [False, True])
def test_legacy_transport_capability_does_not_invent_non_schema_support(legacy_support: bool) -> None:
    from nare.loop import check_structured_output

    inner = ScriptedTransport('{}')
    inner.supports_structured_output = legacy_support
    wrapped = BudgetTransport(inner, BudgetBridge(AttemptBudget(), phase='review'),
        TransportContext(session_id='fixture', before_send=save))
    assert wrapped.structured_output_modes == (('json_schema',) if legacy_support else ())
    with pytest.raises(ValueError):
        check_structured_output(wrapped, {'type': 'object'}, 'json_object')


@pytest.mark.asyncio
@pytest.mark.parametrize('advertised', [False, True])
async def test_configured_unsupported_encoding_has_named_refusal_without_a_call(
    monkeypatch: pytest.MonkeyPatch, advertised: bool,
) -> None:
    from coordinare.services.agent_runtime.configuration import AgentModelConfig
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    inner = ScriptedTransport('{}')
    inner.supports_structured_output = True
    if advertised:
        inner.structured_output_modes = MODES
    monkeypatch.setattr(AgentModelConfig, 'transport', lambda self, instructions: inner)
    config = resolve_conducting(ConductingConfig(backend='openai_api', structured_transport='tool_call'),
                              environ={'OPENAI_API_KEY': 'fixture-key'})
    req = request(admit)
    result = await NareRuntime(config=config).run(req)
    assert result.status == 'held' and result.hold_code == 'agent_transport_unsupported'
    assert req.budget.calls == 0 and inner.requests == []

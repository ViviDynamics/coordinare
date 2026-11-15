"""Domain admission must control the result of the real nare loop."""
from __future__ import annotations

from typing import Any

import pytest
from nare import Reply, Usage

from coordinare.services.environment_attempt import AttemptBudget


class ScriptedTransport:
    """Only inference is substituted; tools, schema and admission run for real."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[dict[str, Any]] = []

    async def context_window(self) -> int:
        return 262144

    async def turn(self, messages: Any, tools: Any) -> Reply:
        self.requests.append({'messages': messages.copy(), 'tools': tools})
        return Reply([{'type': 'text', 'text': self.text}], [], Usage(input=11, output=7), 'end_turn')


def request(check: Any) -> Any:
    from coordinare.services.agent_runtime.models import AgentIdentity, AgentRequest

    return AgentRequest(
        identity=AgentIdentity(attempt_id='attempt-a', phase='review', role='reviewer', source_commit='a' * 40,
                               domain_digests=(('manifest', 'b' * 64),), policy_digest='c' * 64,
                               prompt_digest='d' * 64, schema_digest='e' * 64, model_config_digest='f' * 64),
        instructions='Judge the evidence using the supplied rules.', task='Review this contract.',
        answer_schema={'type': 'object', 'properties': {'accepted': {'type': 'boolean'}},
                       'required': ['accepted'], 'additionalProperties': False},
        check=check, budget=AttemptBudget(max_calls=3, max_schema_repairs=0),
    )


@pytest.mark.asyncio
async def test_accepted_requires_domain_check() -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    checked = []

    async def admit(answer: Any) -> list[Any]:
        checked.append(answer)
        return []

    transport = ScriptedTransport('{"accepted": true}')
    result = await NareRuntime(transport=transport).run(request(admit))
    assert checked == [{'accepted': True}]
    assert result.status == 'accepted'
    assert result.answer == {'accepted': True}
    assert result.usage['input_tokens'] == 11
    # Accidentally enabling nare built-ins would grant host access.
    assert transport.requests[0]['tools'] == []


@pytest.mark.asyncio
async def test_failed_check_never_returns_accepted() -> None:
    from coordinare.services.agent_runtime.models import AdmissionIssue
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    async def reject(answer: Any) -> list[Any]:
        return [AdmissionIssue(code='missing_receipt', message='Execution proof is absent.')]

    req = request(reject)
    result = await NareRuntime(transport=ScriptedTransport('{"accepted": true}')).run(req)
    assert result.status == 'held'
    assert result.answer is None
    assert result.hold_code == 'attempt_schema_limit'
    assert 'missing_receipt' in req.budget.validation_codes
    assert req.budget.schema_repairs == req.budget.max_schema_repairs


@pytest.mark.asyncio
async def test_check_exception_is_terminal_without_exposing_exception_text() -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    async def broken(answer: Any) -> list[Any]:
        raise ValueError('private-source-content')

    result = await NareRuntime(transport=ScriptedTransport('{"accepted": true}')).run(request(broken))
    assert result.status == 'error'
    assert result.answer is None
    assert result.hold_code == 'agent_check_failed'
    assert 'private-source-content' not in repr(result)


@pytest.mark.asyncio
async def test_configured_request_reaches_native_transport_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    import httpx2

    from coordinare.config import ConductingConfig
    from coordinare.services.agent_runtime.configuration import resolve_conducting
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    sent = []

    def provider(incoming: httpx2.Request) -> httpx2.Response:
        if incoming.method == 'GET':
            return httpx2.Response(200, json={'data': [{'model_name': 'fixture/native',
                'model_info': {'max_input_tokens': 262144}}]})
        assert str(incoming.url) == 'https://model.example/v1/chat/completions'
        assert incoming.headers['authorization'] == 'Bearer fixture-key'
        sent.append(json.loads(incoming.content))
        return httpx2.Response(200, json={'choices': [{'message': {'role': 'assistant',
            'content': '{"accepted":true}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7}})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(provider))
    monkeypatch.setattr(httpx2, 'AsyncClient', lambda **kwargs: client)

    async def admit(answer: Any) -> list[Any]:
        return []

    config = resolve_conducting(ConductingConfig(backend='openai_api', model='fixture/native',
        base_url='https://model.example/v1', max_tokens=16384, temperature=0.2, effort='high'),
        environ={'OPENAI_API_KEY': 'fixture-key'})
    try:
        result = await NareRuntime(config=config).run(request(admit))
    finally:
        await client.aclose()
    assert result.status == 'accepted'
    assert len(sent) == 1
    assert sent[0]['model'] == 'fixture/native'
    assert sent[0]['max_tokens'] == 16384
    assert sent[0]['temperature'] == 0.2
    assert sent[0]['reasoning_effort'] == 'high'
    assert sent[0]['messages'][0] == {'role': 'system', 'content': 'Judge the evidence using the supplied rules.'}


@pytest.mark.asyncio
async def test_owned_transport_cleanup_failure_is_terminal_and_does_not_escape(monkeypatch: Any, tmp_path: Any) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.agent_runtime.configuration import AgentModelConfig, resolve_conducting
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    async def close() -> None:
        raise RuntimeError('private cleanup error')
    async def admit(answer: Any) -> list[Any]:
        return []
    events = []
    async def event(value: Any) -> None:
        events.append(value)
    transport = ScriptedTransport('{"accepted":true}')
    transport._client = SimpleNamespace(aclose=close)
    monkeypatch.setattr(AgentModelConfig, 'transport', lambda *args: transport)
    from coordinare.config import ConductingConfig
    model = resolve_conducting(ConductingConfig(backend='openai_api'), environ={'OPENAI_API_KEY': 'fixture'})
    req = replace(request(admit), checkpoint_store=CheckpointStore(tmp_path), event_sink=event)
    result = await NareRuntime(config=model).run(req)
    assert result.status == 'held' and result.hold_code == 'agent_transport_cleanup'
    assert result.answer is None
    saved = await req.checkpoint_store.load_verified(result.checkpoint_key, req.identity)
    assert saved.hold_code == 'agent_transport_cleanup'
    assert events[-1]['kind'] == 'terminal' and events[-1]['hold_code'] == 'agent_transport_cleanup'
    assert 'private cleanup error' not in repr(result) + repr(events)

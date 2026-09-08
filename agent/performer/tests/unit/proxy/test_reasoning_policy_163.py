"""163: real proxy front doors carry policy to the correct upstream model."""
from __future__ import annotations

import json

import httpx
import pytest

from performer.proxy.dual_model_proxy import DualModelProxy


@pytest.mark.asyncio
@pytest.mark.parametrize('wire', ['messages', 'chat/completions', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
async def test_policy_reaches_upstream_from_every_wire(wire, stream, monkeypatch):
    captured = []
    monkeypatch.setenv('POLICY_TEST_KEY', 'test-key')

    def handler(request):
        captured.append(json.loads(request.content))
        assert request.headers['authorization'] == 'Bearer test-key'
        return httpx.Response(200, json={'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = DualModelProxy(orchestration={'strategy': 'single', 'tool': {
        'model': 'ada/qwen3-14b', 'base_url': 'http://gateway/v1',
        'auth_env': 'POLICY_TEST_KEY', 'reasoning_policy': 'disable_thinking',
    }}, client=client)
    base = await proxy.start()
    body = {'model': 'cli-model', 'stream': stream, 'max_tokens': 128,
            'messages': [{'role': 'user', 'content': 'go'}]}
    if wire == 'messages':
        body['system'] = [{'type': 'text', 'text': 'Keep the persona', 'cache_control': {'type': 'ephemeral'}}]
    if wire == 'responses':
        body.pop('messages'); body.pop('max_tokens')
        body.update(input='go', max_output_tokens=128)
    try:
        async with httpx.AsyncClient() as caller:
            response = await caller.post(f'{base}/{wire}', json=body)
        assert response.status_code == 200
        assert captured[0]['chat_template_kwargs'] == {'enable_thinking': False}
        assert captured[0]['max_tokens'] == 128
        assert captured[0]['model'] == 'ada/qwen3-14b'
        assert captured[0]['messages'][-1]['content'] == 'go'
        if wire == 'messages':
            assert captured[0]['messages'][0] == {'role': 'system', 'content': 'Keep the persona'}
        assert 'done' in response.text
    finally:
        await proxy.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_executor_policy_does_not_change_planner_request():
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'answer'}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = DualModelProxy(orchestration={'strategy': 'always',
        'tool': {'model': 'qwen', 'base_url': 'http://gateway/v1', 'reasoning_policy': 'disable_thinking'},
        'thinking': {'model': 'glm', 'base_url': 'http://gateway/v1'},
    }, client=client)
    base = await proxy.start()
    try:
        async with httpx.AsyncClient() as caller:
            result = await caller.post(f'{base}/chat/completions', json={'messages': [{'role': 'user', 'content': 'go'}]})
        assert result.status_code == 200
        assert [body['model'] for body in captured] == ['glm', 'qwen']
        assert 'chat_template_kwargs' not in captured[0]
        assert captured[1]['chat_template_kwargs'] == {'enable_thinking': False}
    finally:
        await proxy.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_policy_launch_overrides_reroute_without_dropping_repairs(monkeypatch):
    from performer.proxy.launch import maybe_launch_proxy
    from performer.proxy.routing import RoutingEntry, RoutingTable, TargetDescriptor

    async def start(self):
        return 'http://127.0.0.1:12345'

    monkeypatch.setattr(DualModelProxy, 'start', start)
    route = RoutingTable(entries=[RoutingEntry(backend='codex', model='qwen', target=TargetDescriptor(
        base_url='http://gateway/v1', wire_format='openai', strategy='normalize',
        normalizers=['strip_reasoning'], upstream_auth_env='ROUTE_KEY',
    ))])
    env = {'CODEX_PROVIDER_BASE_URL': 'http://original'}
    proxy = await maybe_launch_proxy({'strategy': 'single', 'tool': {
        'model': 'qwen', 'reasoning_policy': 'disable_thinking', 'base_url': 'http://original',
    }}, 'codex', env, routing_table=route, model='qwen')
    assert isinstance(proxy, DualModelProxy)
    assert proxy.orchestration['tool']['normalizers'] == ['strip_reasoning']
    assert proxy.orchestration['tool']['auth_env'] == 'ROUTE_KEY'
    assert proxy.orchestration['tool']['wire_format'] == 'openai'
    assert proxy.orchestration['tool']['auth_style'] == 'bearer'
    assert proxy.orchestration['tool']['preserve_generation'] is True
    assert env['CODEX_PROVIDER_BASE_URL'] == 'http://127.0.0.1:12345'
    await proxy.stop()
    assert env['CODEX_PROVIDER_BASE_URL'] == 'http://original'

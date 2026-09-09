"""262: GLM reasoning-only and tool replies survive the LiteLLM translate route."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from performer.proxy.health import _translate_round_trip_has_tool_use, check_health
from performer.proxy.normalizers import NORMALIZER_REGISTRY
from performer.proxy.normalizers.reasoning import StripReasoningNormalizer
from performer.proxy.routing import TargetDescriptor
from performer.proxy.shim import SelfHostedShim

MODEL = 'glm-5.3-flash'


def _target():
    doc = yaml.safe_load((Path(__file__).parents[5] / 'routing.example.yaml').read_text())
    entry = next(row for row in doc['selfhosted_routing'] if row['backend'] == 'claude_code' and row['model'] == MODEL)
    target = TargetDescriptor.model_validate(entry['target'])
    assert target.strategy == 'translate'
    assert target.upstream_model == MODEL
    assert target.upstream_auth_env == 'COORDINARE_PROXY_AUTH'
    return target


@pytest.mark.asyncio
async def test_completion_probe_judges_reasoning_only_glm_after_normalizers():
    def handler(request):
        assert request.url.path == '/v1/chat/completions'
        assert json.loads(request.content)['model'] == MODEL
        return httpx.Response(200, json={'id': 'glm', 'choices': [{'message': {'content': '', 'reasoning_content': 'ready'}, 'finish_reason': 'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_health(_target(), model=MODEL, client=client)
    assert result.resolved_action == 'proceed'


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_real_translate_front_door_promotes_glm_and_preserves_auth(monkeypatch, stream):
    monkeypatch.setenv('COORDINARE_PROXY_AUTH', 'test-proxy-key')
    def handler(request):
        assert request.headers['authorization'] == 'Bearer test-proxy-key'
        assert request.url.path == '/v1/chat/completions'
        assert json.loads(request.content)['model'] == MODEL
        if stream:
            frames = [{'id': 'glm', 'model': MODEL, 'choices': [{'index': 0, 'delta': {'reasoning_content': 'ready'}, 'finish_reason': None}]},
                      {'id': 'glm', 'model': MODEL, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
            return httpx.Response(200, content=''.join('data: '+json.dumps(frame)+'\n\n' for frame in frames)+'data: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json={'id': 'glm', 'model': MODEL, 'choices': [{'message': {'role': 'assistant', 'content': '', 'reasoning_content': 'ready'}, 'finish_reason': 'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        shim = SelfHostedShim(target=_target(), normalizers=[NORMALIZER_REGISTRY[key] for key in _target().normalizers], client=upstream)
        base = await shim.start()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(base+'/v1/messages', json={'model': MODEL, 'max_tokens': 128, 'stream': stream,
                                                                       'messages': [{'role': 'user', 'content': 'Reply ready'}]})
            assert response.status_code == 200
            if stream:
                assert 'text_delta' in response.text and 'ready' in response.text and 'message_stop' in response.text
            else:
                assert response.json()['content'] == [{'type': 'text', 'text': 'ready'}]
        finally:
            await shim.stop()


def test_glm_tool_use_survives_reasoning_normalization_and_translation():
    body = {'id': 'glm', 'model': MODEL, 'choices': [{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': '', 'reasoning_content': 'I should read the file',
        'tool_calls': [{'id': 'call_glm', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{"file_path":"README.md"}'}}],
    }}]}
    assert _translate_round_trip_has_tool_use(_target(), body)
    normalized = StripReasoningNormalizer().normalize_json(body)
    assert normalized['choices'][0]['message']['tool_calls'][0]['function']['name'] == 'Read'


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [MODEL, "spark" + "/glm-5.3-flash"])
async def test_cli_preserves_the_supplied_model_argument(tmp_path, monkeypatch, model):
    from unittest.mock import AsyncMock

    from performer.backends.claude_code import ClaudeCodeBackend
    from tests.unit.backends.test_claude_code import _fake_proc, _score, _stand

    launch = AsyncMock(return_value=_fake_proc())
    monkeypatch.setattr('performer.backends.claude_code.asyncio.create_subprocess_exec', launch)
    adapter = ClaudeCodeBackend()
    await adapter.start(_stand(tmp_path), _score(), model=model)
    args = list(launch.call_args.args)
    assert args[args.index('--model') + 1] == model

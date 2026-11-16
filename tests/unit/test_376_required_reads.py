"""Only inference is scripted; production phase, nare, receipts and validators run."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from nare import Reply, ToolCall, Usage

from coordinare.services.agent_runtime.evidence import EvidenceSource
from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_agent import PinnedSourceTools, run_environment_phase
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from tests.unit.test_351_environment_discovery import PATH, SOURCE
from tests.unit.test_353_review_findings_flow import finding, review
from tests.unit.test_376_environment_review import phase

NAME = 'continue_required_reads'

class ReadBarrierRuntime(NareRuntime):
    def __init__(self, transport):
        super().__init__(transport=transport)
        self.request = None
        self.result = None

    async def run(self, request):
        self.request = request
        self.result = await super().run(request)
        return self.result


def tool_reply(name, arguments, number):
    call = ToolCall(f'call-{number}', name, arguments)
    return Reply([{'type': 'tool_use', 'id': call.id, 'name': name, 'input': arguments}],
                 [call], Usage(input=100, output=10), 'tool_use')


class EagerProvider:
    required_tool_choice = 'required'
    """Wants to answer every free turn, but obeys required-tool wire selection."""
    def __init__(self, answer=None, *, source=False, obey=True, compact=False, cancel_at=None, streaming=False):
        self.answer = answer or review()
        self.source = source
        self.obey = obey
        self.compact = compact
        self.cancel_at = cancel_at
        self.streaming = streaming
        self.sent = []
        self.received = {}
        self.source_paths = [PATH, "guide"] if source else []

    async def context_window(self):
        return 16000 if self.compact else 262144

    async def turn(self, messages, tools, *, response_schema=None, response_mode=None, tool_choice=None):
        self.sent.append({'tools': tools, 'choice': tool_choice, 'schema': response_schema, 'mode': response_mode})
        for message in messages:
            for block in message.content:
                if block.get('type') == 'tool_result' and isinstance(block.get('content'), str):
                    try:
                        payload = json.loads(block['content'])
                    except ValueError:
                        continue
                    for page in payload.get('pages', []):
                        assert hashlib.sha256(page['content'].encode()).hexdigest() == page['sha256']
                        assert len(block['content']) <= 30000
                        key = (page['source'], page['id'])
                        assert self.received.get(key, page) == page
                        self.received[key] = page
        if len(self.sent) == self.cancel_at:
            raise asyncio.CancelledError
        if self.obey and tool_choice == 'required':
            assert [tool['name'] for tool in tools] == [NAME]
            assert response_schema is None
            return tool_reply(NAME, {}, len(self.sent))
        if self.source_paths:
            return tool_reply('read_source', {'path': self.source_paths.pop(0)}, len(self.sent))
        return Reply([{'type': 'text', 'text': json.dumps(self.answer)}], [], Usage(input=100, output=10), 'end_turn')

    async def stream_turn(self, messages, tools, **kwargs):
        yield await self.turn(messages, tools, **kwargs)


def experiment(tmp_path, provider, *, large=False, source=False, compact=False):
    runtime = ReadBarrierRuntime(provider)
    req = replace(phase(tmp_path, provider), runtime=runtime, phase_timeout_seconds=10)
    req.budget.max_schema_repairs = 0
    if large:
        text = ('arbitrary nested input 漢字 \\ "\n' * 6000)
        extra = EvidenceSource('nested/generic.input', text, hashlib.sha256(text.encode()).hexdigest())
        req = replace(req, sources=(*req.sources, extra), required=req.required | {extra.key})
    if source:
        async def fetch(path):
            return SOURCE if path == PATH else 'arbitrary nested source 漢字\n' * 6500
        req = replace(req, source_tools=PinnedSourceTools(SourceInspection([PATH, 'guide'], fetch, DiscoveryLimits())))
    return req, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_complete_packet_then_answer_with_zero_corrections(tmp_path, streaming):
    provider = EagerProvider(streaming=streaming)
    req, runtime = experiment(tmp_path, provider, large=True)
    result = await run_environment_phase(req)
    assert result.value.verdict == 'accept'
    assert result.evidence.remaining_required == 0
    assert req.budget.schema_repairs == req.budget.agent_evidence_errors == 0
    assert req.budget.calls == req.budget.calls_completed == len(provider.sent)
    assert result.agent.usage['input_tokens'] == 100 * len(provider.sent)
    assert result.agent.usage['output_tokens'] == 10 * len(provider.sent)
    assert len(provider.sent) > 4
    for source in req.sources:
        returned = ''.join(page['content'] for (key, _), page in sorted(provider.received.items()) if key == source.key)
        assert returned == (source.content if source.key in req.required else '')
    assert provider.sent[-1]['choice'] is None
    assert runtime.request.check is not None


@pytest.mark.asyncio
async def test_requested_source_is_drained_before_eager_answer(tmp_path):
    provider = EagerProvider(source=True)
    req, _ = experiment(tmp_path, provider, source=True)
    result = await run_environment_phase(req)
    assert result.value.verdict == 'accept'
    assert not req.source_tools.issues()
    assert req.source_tools.inspection.reads == 2
    store = req.source_tools.catalogs['guide']
    assert len(store.delivered) == store.page_count > 30
    assert req.budget.schema_repairs == 0
    returned = ''.join(page['content'] for (key, _), page in sorted(provider.received.items()) if key == 'guide')
    assert returned == req.source_tools.inspection.source_files['guide']


@pytest.mark.asyncio
async def test_noncompliant_provider_still_hits_original_read_validator(tmp_path):
    provider = EagerProvider(obey=False)
    req, _ = experiment(tmp_path, provider)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(req)
    assert caught.value.code == 'attempt_schema_limit'
    assert 'review_evidence_unread' in req.budget.validation_codes
    assert len(provider.sent) == 1
    assert not provider.received


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', ['revise', 'indeterminate'])
async def test_complete_reads_do_not_change_genuine_verdict(tmp_path, verdict):
    provider = EagerProvider(review(verdict=verdict, findings=[finding()]))
    req, _ = experiment(tmp_path, provider)
    result = await run_environment_phase(req)
    assert result.value.verdict == verdict
    assert result.data == provider.answer['assessment']
    assert req.budget.schema_repairs == 0


@pytest.mark.asyncio
async def test_invalid_domain_answer_remains_rejected_after_reads(tmp_path):
    answer = review()
    answer['assessment']['sources'] = ['unread/fictional']
    provider = EagerProvider(answer)
    req, _ = experiment(tmp_path, provider)
    with pytest.raises(EnvironmentDiscoveryError):
        await run_environment_phase(req)
    assert provider.received
    assert len(provider.sent) > 1


@pytest.mark.asyncio
async def test_call_exhaustion_does_not_silently_drain_remaining_pages(tmp_path):
    provider = EagerProvider()
    req, runtime = experiment(tmp_path, provider, large=True)
    req.budget.max_calls = 2
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(req)
    assert caught.value.code == 'attempt_call_limit'
    assert len(provider.sent) == req.budget.calls == 2
    assert runtime.request.evidence_catalog.remaining_required > 0
    assert req.budget.schema_repairs == 0


@pytest.mark.asyncio
async def test_compaction_does_not_reset_receipts_or_lose_full_delivery(tmp_path):
    provider = EagerProvider(compact=True)
    req, _ = experiment(tmp_path, provider, large=True)
    result = await run_environment_phase(req)
    assert result.value.verdict == 'accept'
    assert req.budget.agent_compactions > 0
    assert result.evidence.remaining_required == 0
    assert len(provider.received) == len(result.evidence.delivered)


@pytest.mark.asyncio
async def test_pending_observation_is_drained_without_creating_another_action(tmp_path):
    from coordinare.services.environment_agent import ObservationTools
    observations = ObservationTools()
    key = observations.register({'opaque_output': 'untrusted generic observation 漢字' * 5000})
    provider = EagerProvider()
    req, _ = experiment(tmp_path, provider)
    result = await run_environment_phase(replace(req, observations=observations))
    assert result.value.verdict == 'accept'
    assert not observations.issues()
    assert len(observations.catalogs) == 1
    assert observations.catalogs[key].remaining_required == 0
    assert req.budget.trial_runs == req.budget.probe_batches == 0


@pytest.mark.asyncio
async def test_verified_cancel_resume_preserves_usage_receipts_and_absolute_deadline(tmp_path):
    provider = EagerProvider(cancel_at=3)
    req, runtime = experiment(tmp_path, provider, large=True)
    with pytest.raises(asyncio.CancelledError):
        await run_environment_phase(req)
    saved = await req.checkpoint_store.load_verified(runtime.result.checkpoint_key, runtime.request.identity)
    assert saved.terminal_status == 'cancelled'
    assert 0 < len(saved.evidence.delivered) < runtime.request.evidence_catalog.page_count
    assert req.budget.calls == 3 and req.budget.calls_completed == 2
    assert req.budget.agent_usage_unknown == 1
    before_calls = req.budget.calls
    second = EagerProvider()
    resumed_runtime = ReadBarrierRuntime(second)
    resumed = await run_environment_phase(replace(req, runtime=resumed_runtime, checkpoint_key=runtime.result.checkpoint_key))
    final = await req.checkpoint_store.load_verified(resumed.agent.checkpoint_key, resumed_runtime.request.identity)
    assert final.phase_expires_at == saved.phase_expires_at
    assert set(saved.evidence.delivered) <= set(final.evidence.delivered)
    assert resumed.agent.usage['input_tokens'] == (2 + len(second.sent)) * 100
    assert req.budget.calls == before_calls + len(second.sent)
    assert req.budget.agent_usage_unknown == 1 and req.budget.schema_repairs == 0
    assert resumed.value.verdict == 'accept'


@pytest.mark.asyncio
async def test_policy_change_and_byte_tampering_refuse_checkpoint(tmp_path):
    provider = EagerProvider(cancel_at=2)
    req, runtime = experiment(tmp_path, provider, large=True)
    with pytest.raises(asyncio.CancelledError):
        await run_environment_phase(req)
    saved = await req.checkpoint_store.load_verified(runtime.result.checkpoint_key, runtime.request.identity)
    with pytest.raises(EnvironmentDiscoveryError):
        await req.checkpoint_store.load_verified(runtime.result.checkpoint_key,
            replace(saved.identity, policy_digest='0' * 64))
    path = tmp_path / runtime.result.checkpoint_key
    # Content-addressed object, not the native session's counters, is corrupted.
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(EnvironmentDiscoveryError):
        await req.checkpoint_store.load_verified(runtime.result.checkpoint_key, runtime.request.identity)


@pytest.mark.asyncio
async def test_expired_attempt_does_not_read_or_send(tmp_path):
    provider = EagerProvider()
    req, _ = experiment(tmp_path, provider)
    req.budget.started -= req.budget.timeout_seconds + 1
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(req)
    assert caught.value.code == 'attempt_deadline'
    assert not provider.sent and not provider.received


@pytest.mark.asyncio
async def test_unknown_required_tool_semantics_refuse_before_inference(tmp_path):
    provider = EagerProvider()
    provider.required_tool_choice = None
    req, _ = experiment(tmp_path, provider)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(req)
    assert caught.value.code == 'agent_required_tools_unsupported'
    assert not provider.sent and req.budget.calls == 0


@pytest.mark.asyncio
async def test_dynamic_source_resume_uses_verified_partial_catalog_without_fetching_again(tmp_path):
    provider = EagerProvider(source=True, cancel_at=5)
    req, runtime = experiment(tmp_path, provider, source=True)
    with pytest.raises(asyncio.CancelledError):
        await run_environment_phase(req)
    store = req.source_tools.catalogs['guide']
    before = store.receipt()
    assert 0 < len(before.delivered) < store.page_count
    second = EagerProvider()
    async def must_not_fetch(path):
        raise AssertionError('Resume must use verified snapshot bytes')
    new_navigation = PinnedSourceTools(SourceInspection([PATH, 'guide'], must_not_fetch, DiscoveryLimits()))
    resumed_runtime = ReadBarrierRuntime(second)
    resumed = await run_environment_phase(replace(req, runtime=resumed_runtime,
        source_tools=new_navigation, checkpoint_key=runtime.result.checkpoint_key))
    assert resumed.value.verdict == 'accept'
    assert new_navigation.inspection.reads == req.source_tools.inspection.reads
    assert new_navigation.inspection.source_files['guide'] == req.source_tools.inspection.source_files['guide']
    assert not new_navigation.issues()
    assert before.catalog_digest == new_navigation.catalogs['guide'].receipt().catalog_digest
    assert len(new_navigation.catalogs['guide'].delivered) == store.page_count


@pytest.mark.asyncio
async def test_noncompliant_provider_cannot_clear_dynamic_source_debt(tmp_path):
    provider = EagerProvider(source=True)
    req, runtime = experiment(tmp_path, provider, source=True)
    original_turn = provider.turn
    async def misbehave(messages, tools, **kwargs):
        if 'guide' in req.source_tools.catalogs:
            provider.obey = False
        return await original_turn(messages, tools, **kwargs)
    provider.turn = misbehave
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(req)
    assert caught.value.code == 'attempt_schema_limit'
    assert req.source_tools.catalogs['guide'].remaining_required > 0
    assert 'review_evidence_unread' in req.budget.validation_codes
    assert runtime.result.answer is None


@pytest.mark.asyncio
async def test_held_checkpoint_is_not_reopened_by_read_controller(tmp_path):
    provider = EagerProvider(obey=False)
    req, runtime = experiment(tmp_path, provider)
    with pytest.raises(EnvironmentDiscoveryError):
        await run_environment_phase(req)
    second = EagerProvider()
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(replace(req, runtime=ReadBarrierRuntime(second),
            checkpoint_key=runtime.result.checkpoint_key))
    assert caught.value.code == 'attempt_schema_limit'
    assert not second.sent


@pytest.mark.asyncio
async def test_tool_name_collision_refuses_without_sending_or_reading(tmp_path):
    from nare import Tool
    provider = EagerProvider()
    req, _ = experiment(tmp_path, provider)
    async def must_not_run(arguments):
        raise AssertionError('Must never execute')
    collision = Tool(name=NAME, description='Conflicting tool', input_schema={'type': 'object'}, call=must_not_run)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(replace(req, tools=(collision,)))
    assert caught.value.code == 'agent_prerequisite_tool_collision'
    assert not provider.sent


@pytest.mark.asyncio
async def test_automatic_resume_finds_policy_bound_checkpoint_without_reset(tmp_path):
    provider = EagerProvider(cancel_at=3)
    req, runtime = experiment(tmp_path, provider, large=True)
    with pytest.raises(asyncio.CancelledError):
        await run_environment_phase(req)
    key = runtime.result.checkpoint_key
    second = EagerProvider()
    continued = ReadBarrierRuntime(second)
    result = await run_environment_phase(replace(req, runtime=continued))
    assert continued.request.checkpoint_key == key
    assert result.agent.usage['input_tokens'] == (2 + len(second.sent)) * 100
    assert req.budget.calls_completed == 2 + len(second.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['json_schema', 'json_object', 'tool_call'])
@pytest.mark.parametrize('streaming', [False, True])
async def test_actual_openai_wire_separates_read_and_selected_answer_encoding(mode, streaming):
    import httpx2
    from nare.transport.openai import OpenAITransport

    from coordinare.services.agent_runtime.accounting import (
        BudgetBridge,
        BudgetTransport,
        TransportContext,
    )
    from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceStore
    from coordinare.services.agent_runtime.required_reads import RequiredReads
    from tests.unit.test_376_agent_runtime import request

    sent = []
    def provider(incoming):
        body = json.loads(incoming.content)
        sent.append(body)
        first = len(sent) == 1
        name = NAME if first else 'nare_final_answer'
        arguments = '{}' if first else '{"accepted":true}'
        is_tool = first or mode == 'tool_call'
        message = ({'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': f'wire-{len(sent)}', 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}]} if is_tool else
            {'role': 'assistant', 'content': arguments})
        finish = 'tool_calls' if is_tool else 'stop'
        usage = {'prompt_tokens': 100, 'completion_tokens': 10}
        if not body.get('stream'):
            return httpx2.Response(200, json={'choices': [{'message': message, 'finish_reason': finish}], 'usage': usage})
        delta = dict(message)
        if is_tool:
            delta['tool_calls'] = [{'index': 0, **message['tool_calls'][0]}]
        chunks = [{'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                  {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}], 'usage': usage}]
        content = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
        return httpx2.Response(200, content=content, headers={'content-type': 'text/event-stream'})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(provider))
    inner = OpenAITransport(model='fixture/native', api_key='fixture-key', base_url='https://model.example/v1',
                            system='Read required facts and return the declared JSON answer.', http_client=client)
    content = 'required immutable input'
    catalog = EvidenceStore((EvidenceSource('nested/input', content, hashlib.sha256(content.encode()).hexdigest()),),
                            required=frozenset({'nested/input'}))
    async def check(answer):
        return []
    req = replace(request(check), evidence_catalog=catalog)
    async def read():
        return (await catalog.read(EvidenceRead())).serialized
    async def saved():
        pass
    barrier = RequiredReads(lambda: read if catalog.remaining_required else None, req.budget.check)
    barrier.validate_transport(inner)
    wrapped = BudgetTransport(inner, BudgetBridge(req.budget, phase='review'),
        TransportContext(session_id='wire', before_send=saved, request_stage=barrier))
    async def call():
        options = {'response_schema': req.answer_schema, 'response_mode': mode}
        if streaming:
            events = [event async for event in wrapped.stream_turn([], [], **options)]
            return next(event for event in events if isinstance(event, Reply))
        return await wrapped.turn([], [], **options)
    try:
        first = await call()
        assert first.tool_calls[0].name == NAME
        await barrier.tool.call(first.tool_calls[0].args)
        final = await call()
    finally:
        await client.aclose()
    assert first.usage.input == final.usage.input == 100
    assert sent[0]['tool_choice'] == 'required'
    assert [tool['function']['name'] for tool in sent[0]['tools']] == [NAME]
    assert 'response_format' not in sent[0]
    if mode == 'tool_call':
        assert sent[1]['tool_choice'] == {'type': 'function', 'function': {'name': 'nare_final_answer'}}
        assert sent[1]['parallel_tool_calls'] is False
    else:
        assert not sent[1].get('tools')
        assert sent[1]['response_format']['type'] == mode
        if mode == 'json_schema':
            assert sent[1]['response_format']['json_schema']['schema'] == req.answer_schema
    assert catalog.remaining_required == 0




@pytest.mark.asyncio
async def test_anthropic_non_thinking_required_read_uses_vendor_any_choice():
    import httpx2
    from nare.transport.anthropic import AnthropicTransport

    from coordinare.services.agent_runtime.required_reads import RequiredReads
    sent = []
    def provider(incoming):
        sent.append(json.loads(incoming.content))
        return httpx2.Response(200, json={'id': 'fixture-message', 'type': 'message', 'role': 'assistant',
            'model': 'fixture/native', 'content': [{'type': 'tool_use', 'id': 'read-1', 'name': NAME, 'input': {}}],
            'stop_reason': 'tool_use', 'stop_sequence': None, 'usage': {'input_tokens': 30, 'output_tokens': 5}})
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(provider))
    transport = AnthropicTransport(model='fixture/native', api_key='fixture-key',
                                  base_url='https://model.example', http_client=client)
    async def read():
        return '{}'
    stage = RequiredReads(lambda: read, lambda: None)
    try:
        stage.validate_transport(transport)
        selected = stage.select([], {'type': 'object'}, 'json_schema', None)
        reply = await transport.turn([], selected.tools, tool_choice=selected.tool_choice)
    finally:
        await client.aclose()
    assert sent[0]['tool_choice'] == {'type': 'any'}
    assert [tool['name'] for tool in sent[0]['tools']] == [NAME]
    assert 'thinking' not in sent[0] and 'output_config' not in sent[0]
    assert reply.tool_calls[0].name == NAME and reply.usage.input == 30


@pytest.mark.parametrize('thinking,streaming', [(True, False), (False, True)])
def test_unverified_anthropic_modes_refuse_without_altering_configuration(thinking, streaming):
    from nare.transport.anthropic import AnthropicTransport

    from coordinare.services.agent_runtime.required_reads import RequiredReads
    transport = object.__new__(AnthropicTransport)
    transport.thinking = {'type': 'adaptive'} if thinking else None
    transport.streaming = streaming
    stage = RequiredReads(lambda: None, lambda: None)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        stage.validate_transport(transport)
    assert caught.value.code == 'agent_required_tools_unsupported'
    assert transport.thinking == ({'type': 'adaptive'} if thinking else None)
    assert transport.streaming is streaming

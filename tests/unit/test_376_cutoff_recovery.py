"""Unfinished replies spend existing corrections, never another retry budget."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest
from nare import Reply, Tool, ToolCall, Usage, loads

from coordinare.services.agent_runtime.checkpoint import CheckpointStore
from coordinare.services.agent_runtime.models import AdmissionIssue
from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.test_376_agent_runtime import ScriptedTransport, request


class Replies(ScriptedTransport):
    def __init__(self, replies):
        super().__init__('')
        self.replies = iter(replies)

    async def turn(self, messages, tools, **kwargs):
        self.requests.append({'messages': messages.copy(), 'tools': tools})
        return next(self.replies)


def reply(reason='end_turn', text='{"accepted":true}', calls=()):
    return Reply([{'type': 'text', 'text': text}] if text else [], list(calls),
                 Usage(input=11, output=7), reason)


async def admit(answer):
    return []


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['repetition', 'max_tokens'])
@pytest.mark.parametrize('shard', [False, True])
async def test_cutoff_recovers_with_one_existing_correction(reason, shard):
    budget = AttemptBudget(max_calls=2, max_schema_repairs=1)
    transport = Replies([reply(reason), reply()])
    checked = []

    async def check(answer):
        checked.append(answer)
        return []

    result = await NareRuntime(transport=transport).run(replace(
        request(check), budget=budget, shard_corrections=1 if shard else None))
    assert result.status == 'accepted'
    assert checked == [{'accepted': True}]  # Cut-off JSON cannot pass admission.
    assert (budget.shard_schema_repairs if shard else budget.schema_repairs) == 1
    assert budget.calls == budget.calls_completed == 2
    assert result.usage['input_tokens'] == 22 and result.usage['output_tokens'] == 14
    assert budget.call_stats['review']['input_tokens'] == 22
    assert budget.call_stats['review']['output_tokens'] == 14
    assert budget.agent_retries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('shard', [False, True])
@pytest.mark.parametrize('reasons', [
    ['repetition', 'end_turn'], ['end_turn', 'max_tokens'], ['repetition', 'max_tokens'],
])
async def test_schema_and_cutoffs_share_one_allowance(shard, reasons):
    budget = AttemptBudget(max_calls=4, max_schema_repairs=1)
    transport = Replies([*(reply(reason, '{}') for reason in reasons), reply()])
    result = await NareRuntime(transport=transport).run(replace(
        request(admit), budget=budget, shard_corrections=1 if shard else None))
    assert result.status == 'held' and result.answer is None
    assert budget.calls == budget.calls_completed == 2
    assert (budget.shard_schema_repairs if shard else budget.schema_repairs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(('reason', 'code'), [
    ('repetition', 'agent_output_repetition'), ('max_tokens', 'agent_output_truncated'),
    ('refusal', 'agent_provider_refusal'),
])
async def test_zero_allowance_preserves_terminal_reason_and_usage(reason, code):
    req = request(admit)
    result = await NareRuntime(transport=Replies([reply(reason), reply()])).run(req)
    assert result.status == 'held' and result.hold_code == code
    assert req.budget.calls == req.budget.calls_completed == 1
    assert req.budget.schema_repairs == 0
    assert result.usage['output_tokens'] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['repetition', 'max_tokens'])
async def test_streamed_empty_cutoff_uses_same_allowance(reason):
    class Streaming(Replies):
        streaming = True

        async def stream_turn(self, messages, tools, **kwargs):
            yield await self.turn(messages, tools, **kwargs)

    budget = AttemptBudget(max_calls=3, max_schema_repairs=1)
    result = await NareRuntime(transport=Streaming([reply(reason, ''), reply()])).run(
        replace(request(admit), budget=budget))
    assert result.status == 'accepted'
    assert budget.schema_repairs == 1 and budget.calls == 2


@pytest.mark.asyncio
async def test_cutoff_correction_cannot_extend_deadline():
    budget = AttemptBudget(max_calls=4, max_schema_repairs=2)

    async def expire(event):
        if event['kind'] == 'correction':
            budget.started = time.monotonic() - budget.timeout_seconds - 1

    result = await NareRuntime(transport=Replies([reply('repetition'), reply()])).run(
        replace(request(admit), budget=budget, event_sink=expire))
    assert result.status == 'held' and result.hold_code == 'attempt_deadline'
    assert budget.calls == 1 and budget.schema_repairs == 1


@pytest.mark.asyncio
async def test_changed_cutoff_policy_refuses_saved_phase_before_inference(tmp_path, monkeypatch):
    from coordinare.services import environment_agent
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from tests.unit.test_376_required_reads import EagerProvider, ReadBarrierRuntime, experiment

    req, runtime = experiment(tmp_path, EagerProvider(cancel_at=2), large=True)
    with pytest.raises(asyncio.CancelledError):
        await environment_agent.run_environment_phase(req)
    provider = EagerProvider()
    monkeypatch.setattr(environment_agent, 'CUTOFF_POLICY', 'different-recovery-contract')
    with pytest.raises(EnvironmentDiscoveryError) as raised:
        await environment_agent.run_environment_phase(replace(
            req, runtime=ReadBarrierRuntime(provider), checkpoint_key=runtime.result.checkpoint_key))
    assert raised.value.code == 'agent_checkpoint_identity'
    assert provider.sent == []


@pytest.mark.asyncio
async def test_cutoff_correction_does_not_bypass_domain_validator():
    async def reject(answer):
        return [AdmissionIssue(code='missing_proof', message='Execution proof is absent.')]

    budget = AttemptBudget(max_calls=4, max_schema_repairs=1)
    result = await NareRuntime(transport=Replies([reply('repetition'), reply()])).run(
        replace(request(reject), budget=budget))
    assert result.status == 'held' and result.hold_code == 'attempt_schema_limit'
    assert 'missing_proof' in budget.validation_codes
    assert budget.calls == 2 and budget.schema_repairs == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['repetition', 'max_tokens'])
async def test_unfinished_tool_calls_never_execute_but_corrected_call_can(reason):
    effects = []

    async def execute(args):
        effects.append(args['value'])
        return 'done'

    tool = Tool(name='record', description='Record a fixture value.',
                input_schema={'type': 'object', 'properties': {'value': {'type': 'integer'}},
                              'required': ['value']}, call=execute)
    transport = Replies([reply(reason, '', [ToolCall('bad', 'record', {'value': 1})]),
                         reply('tool_use', '', [ToolCall('good', 'record', {'value': 2})]), reply()])
    budget = AttemptBudget(max_calls=3, max_schema_repairs=1)
    result = await NareRuntime(transport=transport).run(
        replace(request(admit), budget=budget, allowed_tools=(tool,)))
    assert result.status == 'accepted'
    assert effects == [2]
    assert budget.schema_repairs == 1 and budget.calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('shard', [False, True])
async def test_cutoff_charge_survives_cancellation_and_resume(tmp_path, shard):
    async def interrupt(event):
        if event['kind'] == 'correction':
            raise asyncio.CancelledError

    budget = AttemptBudget(max_calls=4, max_schema_repairs=1)
    disk = CheckpointStore(tmp_path)
    req = replace(request(admit), budget=budget, shard_corrections=1 if shard else None,
                  checkpoint_store=disk, event_sink=interrupt)
    first = await NareRuntime(transport=Replies([reply('repetition')])).run(req)
    assert first.status == 'cancelled'
    saved = await disk.load_verified(first.checkpoint_key, req.identity)
    assert loads(saved.session_text).cutoffs == 1
    resumed = await NareRuntime(transport=Replies([reply('max_tokens'), reply()])).run(
        replace(req, checkpoint_key=first.checkpoint_key, event_sink=None))
    assert resumed.status == 'held'
    assert budget.calls == 2
    assert (budget.shard_schema_repairs if shard else budget.schema_repairs) == 1


@pytest.mark.asyncio
async def test_cutoff_recovery_cannot_extend_call_limit():
    budget = AttemptBudget(max_calls=1, max_schema_repairs=2)
    result = await NareRuntime(transport=Replies([reply('repetition'), reply()])).run(
        replace(request(admit), budget=budget))
    assert result.status == 'held'
    assert budget.calls == budget.calls_completed == 1
    assert budget.schema_repairs == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('shard', [False, True])
async def test_queued_cutoff_reconciles_before_cancelled_checkpoint_resume(tmp_path, shard):
    async def interrupt(event):
        if event['calls'] == 1 and event['turn'] == 1 and event['kind'] == 'progress':
            raise asyncio.CancelledError

    budget = AttemptBudget(max_calls=4, max_schema_repairs=1)
    disk = CheckpointStore(tmp_path)
    req = replace(request(admit), budget=budget, shard_corrections=1 if shard else None,
                  checkpoint_store=disk, event_sink=interrupt)
    first = await NareRuntime(transport=Replies([reply('repetition')])).run(req)
    assert first.status == 'cancelled'
    saved = await disk.load_verified(first.checkpoint_key, req.identity)
    assert loads(saved.session_text).cutoffs == 1
    assert not any('cutoff' in event.detail for event in loads(saved.session_text).events)
    resumed = await NareRuntime(transport=Replies([reply()])).run(
        replace(req, checkpoint_key=first.checkpoint_key, event_sink=None))
    assert resumed.status == 'accepted'
    assert budget.calls == 2
    assert (budget.shard_schema_repairs if shard else budget.schema_repairs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('shard', [False, True])
async def test_queued_cutoff_cannot_evade_spent_schema_allowance(tmp_path, shard):
    async def interrupt(event):
        if event['calls'] == 2 and event['turn'] == 2 and event['kind'] == 'progress':
            raise asyncio.CancelledError

    budget = AttemptBudget(max_calls=4, max_schema_repairs=1)
    req = replace(request(admit), budget=budget, shard_corrections=1 if shard else None,
                  checkpoint_store=CheckpointStore(tmp_path), event_sink=interrupt)
    first = await NareRuntime(transport=Replies([reply(text='{}'), reply('repetition')])).run(req)
    assert first.status == 'held' and first.hold_code == 'attempt_schema_limit'
    resumed = await NareRuntime(transport=Replies([reply()])).run(
        replace(req, checkpoint_key=first.checkpoint_key, event_sink=None))
    assert resumed.status == 'held' and budget.calls == 2
    assert (budget.shard_schema_repairs if shard else budget.schema_repairs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_provider_detected_repetition_recovers_through_real_wire_adapter(streaming):
    import httpx2
    from nare.transport.openai import OpenAITransport

    sent = []

    def provider(incoming):
        if incoming.method == 'GET':
            return httpx2.Response(200, json={'data': [{'model_name': 'fixture/cutoff',
                'model_info': {'max_input_tokens': 262144}}]})
        body = json.loads(incoming.content)
        sent.append(body)
        text = 'xy' * 2048 if len(sent) == 1 else '{"accepted":true}'
        message = {'role': 'assistant', 'content': text}
        usage = {'prompt_tokens': 100, 'completion_tokens': 30}
        if not body.get('stream'):
            return httpx2.Response(200, json={'choices': [{'message': message, 'finish_reason': 'stop'}], 'usage': usage})
        chunks = [{'choices': [{'index': 0, 'delta': message, 'finish_reason': None}]},
                  {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage}]
        content = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
        return httpx2.Response(200, content=content, headers={'content-type': 'text/event-stream'})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(provider)) as client:
        transport = OpenAITransport(model='fixture/cutoff', api_key='fixture-key',
                                   base_url='https://model.example/v1', http_client=client, streaming=streaming)
        budget = AttemptBudget(max_calls=3, max_schema_repairs=1)
        result = await NareRuntime(transport=transport).run(replace(request(admit), budget=budget))
    assert result.status == 'accepted' and result.answer == {'accepted': True}
    assert budget.calls == budget.calls_completed == 2
    assert budget.schema_repairs == 1 and budget.agent_retries == 0
    assert budget.call_stats['review']['repetition_stops'] == 1
    # Streaming repetition is cut off before the trailing usage chunk arrives.
    # Preserve that unknown charge rather than inventing a token count.
    assert budget.call_stats['review']['output_tokens'] == (30 if streaming else 60)
    assert budget.agent_usage_unknown == (1 if streaming else 0)

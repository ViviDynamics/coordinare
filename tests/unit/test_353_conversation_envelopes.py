"""Parallel author corrections keep their own envelope while sharing resource budgets."""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import TypeAdapter, ValidationError

from coordinare.services import environment_discovery as discovery
from coordinare.services.environment_attempt import AttemptBudget, DiscoveryChatReply
from coordinare.services.environment_shard_scope import AgentContext, AgentTask, run_agent


async def unused_fetch(_path):
    raise AssertionError('Protocol replay needs no source access')


def validate_manifest(data):
    if not data['valid']:
        raise ValidationError.from_exception_data('Global', [
            {'type': 'dict_type', 'loc': ('activation', 'env'), 'input': 'invalid'}])
    return data


@pytest.mark.asyncio
async def test_overlapping_check_author_correction_does_not_change_global_manifest_feedback():
    budget = AttemptBudget(max_schema_repairs=1, max_calls=4)
    limits = discovery.DiscoveryLimits()
    global_waiting, check_finished = asyncio.Event(), asyncio.Event()
    global_messages = [{'role': 'system', 'content': 'global'}, {'role': 'user', 'content': '{}'}]
    check_messages = [{'role': 'system', 'content': 'checks'}, {'role': 'user', 'content': '{}'}]
    global_calls = check_calls = 0
    observed = {}

    async def global_chat(messages):
        nonlocal global_calls
        global_calls += 1
        if global_calls == 1:
            global_waiting.set()
            await check_finished.wait()
            return json.dumps({'manifest': {'valid': False}})
        observed['global'] = json.loads(messages[-1]['content'])
        return json.dumps({'manifest': {'valid': True}})

    async def check_chat(messages):
        nonlocal check_calls
        await global_waiting.wait()
        check_calls += 1
        if check_calls == 1:
            return json.dumps({'shard_checks': None})
        observed['check'] = json.loads(messages[-1]['content'])
        return json.dumps({'shard_checks': {}})

    async def checks():
        inspection = discovery._Inspection('executable_checks', [], unused_fetch, limits, budget)
        task = AgentTask('shard_checks', lambda reply: TypeAdapter(dict).validate_python(reply['shard_checks']),
                         listing_only=True)
        result = await run_agent(AgentContext(budget, limits, check_chat), inspection, check_messages, task)
        check_finished.set()
        return result

    inspection = discovery._Inspection('discovery', [], unused_fetch, limits, budget)
    async with asyncio.timeout(5):
        result, _checks = await asyncio.gather(discovery._phase(inspection=inspection, messages=global_messages,
            chat=global_chat, budget=budget, validate=validate_manifest), checks())
    assert result[0] == {'valid': True}
    assert observed['check']['expected_envelope'] == 'shard_checks'
    assert observed['global']['expected_envelope'] == 'manifest'
    assert observed['global']['validation_errors'][0]['correction_path'] == ['manifest', 'activation', 'env']
    assert (budget.calls, budget.schema_repairs, budget.shard_schema_repairs) == (4, 1, 1)
    assert (budget.scope_revisions, budget.coverage_revisions) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['not JSON', json.dumps({'manifest': {'valid': False}})])
async def test_phase_feedback_uses_its_request_envelope_after_shared_diagnostic_changes(invalid):
    budget = AttemptBudget(max_schema_repairs=1)
    limits = discovery.DiscoveryLimits()
    inspection = discovery._Inspection('discovery', [], unused_fetch, limits, budget)
    messages = [{'role': 'system', 'content': 'global'}, {'role': 'user', 'content': '{}'}]
    calls = 0
    observed = {}

    async def chat(messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            budget.expected_envelope = 'shard_checks'
            return invalid
        observed.update(json.loads(messages[-1]['content']))
        return json.dumps({'manifest': {'valid': True}})

    await discovery._phase(inspection=inspection, messages=messages, chat=chat, budget=budget,
                           validate=validate_manifest)
    assert observed['expected_envelope'] == 'manifest'
    if invalid == 'not JSON':
        assert 'manifest must occur exactly once' in observed['validation_errors'][0]['guidance']
    else:
        assert observed['validation_errors'][0]['correction_path'] == ['manifest', 'activation', 'env']


@pytest.mark.asyncio
async def test_probe_rejection_keeps_its_reply_finish_reason_across_an_await():
    from tests.unit.test_353_probe_pipeline import request_data

    budget = AttemptBudget(max_schema_repairs=1)
    waiting, other_finished = asyncio.Event(), asyncio.Event()
    inspection = discovery._Inspection('discovery', [], unused_fetch, discovery.DiscoveryLimits(), budget)
    messages = []

    async def probe(_data):
        waiting.set()
        await other_finished.wait()
        raise ValidationError.from_exception_data('Probe', [{'type': 'missing', 'loc': ('commands',), 'input': {}}])

    async def other_reply():
        await waiting.wait()
        budget.record_reply(DiscoveryChatReply('{}', finish_reason='length'))
        other_finished.set()

    inspection.probe = probe
    budget.record_reply(DiscoveryChatReply('{}', finish_reason='stop'))
    async with asyncio.timeout(5):
        await asyncio.gather(discovery._probe_reply(request_data(), '{}', messages, inspection, budget), other_reply())
    feedback = json.loads(messages[-1]['content'])
    assert feedback['expected_envelope'] == 'probe'
    assert [e['code'] for e in feedback['validation_errors']] == ['missing']

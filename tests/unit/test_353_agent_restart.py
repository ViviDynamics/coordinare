"""An agent survives repetition stops and one degraded conversation before the attempt holds.

v31 held when one executable review agent spent its two corrections on a reply stopped for repetition and a
corrupted verdict ("revise}" wrapped in noise): one degraded conversation ended the attempt.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from coordinare.services.environment_attempt import (
    AttemptBudget,
    DiscoveryChatReply,
    EnvironmentDiscoveryError,
)
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_shard_scope import AgentContext, AgentTask, run_agent
from tests.unit.test_353_shard_scope import LIMITS

READ = '{"tool":"read_files","paths":["README.md"]}'
VALID = '{"shard_summary":{"criteria":[]}}'
BAD = '{"shard_summary":{"criteria":"none"}}'


def validate(data):
    if not isinstance(data['shard_summary']['criteria'], list):
        raise EnvironmentDiscoveryError('criteria must be a list', code='invalid_json_envelope')
    return data


async def agent(replies):
    """Runs one agent over scripted replies; a tuple is a reply the transport stopped for repetition."""
    queue = iter(replies)
    conversations: list[list[dict[str, str]]] = []

    async def chat(messages, **_kwargs):
        conversations.append([dict(message) for message in messages])
        reply = next(queue)
        if isinstance(reply, tuple):
            return DiscoveryChatReply(text=reply[0], finish_reason='repetition')
        return DiscoveryChatReply(text=reply, finish_reason='stop')

    async def fetch(_path):
        return '# readme\n'

    budget = AttemptBudget(max_calls=32, max_reads=32)
    inspection = SourceInspection(['README.md'], fetch, LIMITS, budget)
    inspection.name = 'scope_shard_author'
    opening = [{'role': 'system', 'content': 'author'}, {'role': 'user', 'content': '{}'}]
    result = await run_agent(AgentContext(budget, LIMITS, chat), inspection, opening,
                             AgentTask('shard_summary', validate))
    return result, budget, conversations


@pytest.mark.asyncio
async def test_replies_stopped_for_repetition_do_not_spend_schema_corrections():
    result, budget, _ = await agent([READ, ('{"shard_summary":',), ('{"shard_summary":',), BAD, BAD, VALID])
    assert result == {'shard_summary': {'criteria': []}}
    assert budget.shard_schema_repairs == 2
    assert 'agent_restarted' not in budget.validation_codes


@pytest.mark.asyncio
async def test_an_exhausted_conversation_restarts_once_from_its_opening_messages():
    result, budget, conversations = await agent([READ, BAD, BAD, BAD, VALID])
    assert result == {'shard_summary': {'criteria': []}}
    assert budget.validation_codes.count('agent_restarted') == 1
    restarted = conversations[-1]
    assert [message['role'] for message in restarted] == ['system', 'user']
    assert restarted == conversations[0]


@pytest.mark.asyncio
async def test_a_second_exhausted_conversation_holds_the_attempt():
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await agent([READ, BAD, BAD, BAD, BAD, BAD, BAD])
    assert caught.value.code == 'attempt_schema_limit'


def test_each_execution_repair_brings_the_probe_batch_its_relocated_cache_needs():
    """v40: the second permitted execution repair held at once; the initial probe and the first repair had used both."""
    budget = AttemptBudget(max_calls=8, max_reads=8)
    budget.reserve_probe_batch()
    budget.reserve_probe_batch()
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        budget.reserve_probe_batch()
    assert caught.value.code == 'attempt_probe_limit'
    budget.execution_repairs = 1
    budget.reserve_probe_batch()
    budget.execution_repairs = 2
    budget.reserve_probe_batch()
    with pytest.raises(EnvironmentDiscoveryError):
        budget.reserve_probe_batch()


class _Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: str
    rationale: str


class _Decisions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decisions: list[_Decision]


def _envelope(decision):
    return json.dumps({'shard_summary': {'decisions': [decision]}})


TWO = _envelope({})
STATUS_ONLY = _envelope({'status': 'open'})
RATIONALE_ONLY = _envelope({'rationale': 'why'})
COMPLETE = _envelope({'status': 'open', 'rationale': 'why'})


def validate_decisions(data):
    return _Decisions.model_validate(data['shard_summary'])


async def decision_agent(replies):
    queue = iter(replies)
    conversations: list[list[dict[str, str]]] = []

    async def chat(messages, **_kwargs):
        conversations.append([dict(message) for message in messages])
        return DiscoveryChatReply(text=next(queue), finish_reason='stop')

    async def fetch(_path):
        return '# readme\n'

    budget = AttemptBudget(max_calls=32, max_reads=32)
    inspection = SourceInspection(['README.md'], fetch, LIMITS, budget)
    inspection.name = 'scope_shard_author'
    opening = [{'role': 'system', 'content': 'author'}, {'role': 'user', 'content': '{}'}]
    try:
        result = await run_agent(AgentContext(budget, LIMITS, chat), inspection, opening,
                                 AgentTask('shard_summary', validate_decisions))
    except EnvironmentDiscoveryError as exc:
        result = exc
    return result, budget, conversations


@pytest.mark.asyncio
async def test_a_correction_that_reduces_the_errors_is_not_charged():
    """dettmore v40: each correction fixed what it was told and broke another field, exhausting the agent's two."""
    result, budget, conversations = await decision_agent([READ, TWO, STATUS_ONLY, RATIONALE_ONLY, COMPLETE])
    assert isinstance(result, _Decisions)
    assert budget.shard_schema_repairs == 2
    assert budget.validation_codes.count('correction_progress') == 1
    assert 'agent_restarted' not in budget.validation_codes
    # The reply that dropped status again is reminded of the rationale fix it must keep.
    last_correction = json.loads(conversations[-1][-1]['content'])
    assert last_correction['validation_errors'][0]['location'] == ['decisions', 0, 'status']
    assert last_correction['earlier_corrections'] == [{'location': ['decisions', 0, 'rationale'], 'code': 'missing'}]
    assert 'earlier_corrections' in last_correction['instruction']
    first_correction = json.loads(conversations[2][-1]['content'])
    assert 'earlier_corrections' not in first_correction


@pytest.mark.asyncio
async def test_only_a_new_low_in_errors_is_free():
    replies = [READ, TWO, STATUS_ONLY, TWO, STATUS_ONLY,  # exhausted: returning to an earlier low is charged
               TWO, STATUS_ONLY, RATIONALE_ONLY, RATIONALE_ONLY]
    result, budget, _ = await decision_agent(replies)
    assert isinstance(result, EnvironmentDiscoveryError)
    assert result.code == 'attempt_schema_limit'
    assert budget.validation_codes.count('correction_progress') == 2
    assert budget.validation_codes.count('agent_restarted') == 1


class _Ruling(BaseModel):
    model_config = ConfigDict(extra='forbid')
    finding_id: str
    status: str
    rationale: str


class _Rulings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decisions: list[_Ruling]


def _assessment(*decisions):
    return json.dumps({'assessment': {'decisions': list(decisions)}})


@pytest.mark.asyncio
async def test_a_decision_given_earlier_is_kept_when_a_correction_drops_it():
    """Dettmore e2e-52: correcting one decision's missing status, the reviewer dropped status from another, 39 times in
    one generation, until the schema budget ran out. A decision already given is carried; only the named one is redone."""
    queue = iter([READ,
                  _assessment({'finding_id': 'F1', 'status': 'resolved', 'rationale': 'fixed'},
                              {'finding_id': 'F2', 'rationale': 'still open'}),
                  _assessment({'finding_id': 'F1', 'rationale': 'fixed'},
                              {'finding_id': 'F2', 'status': 'open', 'rationale': 'still open'})])

    async def chat(messages, **_kwargs):
        return DiscoveryChatReply(text=next(queue), finish_reason='stop')

    async def fetch(_path):
        return '# readme\n'

    budget = AttemptBudget(max_calls=32, max_reads=32)
    inspection = SourceInspection(['README.md'], fetch, LIMITS, budget)
    inspection.name = 'review'
    result = await run_agent(AgentContext(budget, LIMITS, chat), inspection,
                             [{'role': 'system', 'content': 'review'}, {'role': 'user', 'content': '{}'}],
                             AgentTask('assessment', lambda data: _Rulings.model_validate(data['assessment']),
                                       decision_ids=['F1', 'F2']))
    assert [(d.finding_id, d.status) for d in result.decisions] == [('F1', 'resolved'), ('F2', 'open')]
    assert budget.shard_schema_repairs == 1
    assert budget.validation_codes.count('decision_carried') == 1

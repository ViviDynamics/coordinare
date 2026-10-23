"""An agent survives repetition stops and one degraded conversation before the attempt holds.

v31 held when one executable review agent spent its two corrections on a reply stopped for repetition and a
corrupted verdict ("revise}" wrapped in noise): one degraded conversation ended the attempt.
"""
from __future__ import annotations

import pytest

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

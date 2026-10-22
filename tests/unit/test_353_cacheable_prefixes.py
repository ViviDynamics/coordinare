"""Stable, cacheable prompt prefixes (throughput amendment, section 3)."""
from __future__ import annotations

import json
from collections import defaultdict

import pytest

from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_shard_scope import (
    SHARED_CONTEXT,
    AgentContext,
    AgentTask,
    run_agent,
)
from tests.unit.test_353_shard_scope import LIMITS, Router, run


def shared_prefix(content: str) -> str:
    """The context message up to and including its shared object, which canonical JSON places first."""
    data = json.loads(content)
    head = json.dumps({SHARED_CONTEXT: data[SHARED_CONTEXT]}, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return head[:-1]


@pytest.mark.asyncio
async def test_a_conversation_keeps_one_system_message():
    router = Router()
    await run(router)
    systems: dict[str, set[str]] = defaultdict(set)
    for role, messages in router.conversations:
        if role == 'author':
            systems[json.loads(messages[1]['content'])['shard']['id']].add(messages[0]['content'])
    assert systems and all(len(seen) == 1 for seen in systems.values())


@pytest.mark.asyncio
async def test_every_author_shares_a_byte_identical_prefix_including_the_spine():
    router = Router()
    await run(router)
    authors = [messages for role, messages in router.conversations if role == 'author' and len(messages) == 2]
    assert len(authors) >= 2
    prefixes = {messages[0]['content'] + shared_prefix(messages[1]['content']) for messages in authors}
    assert len(prefixes) == 1
    assert all(messages[1]['content'].startswith(shared_prefix(messages[1]['content'])) for messages in authors)
    assert 'spine_evidence' in json.loads(authors[0][1]['content'])[SHARED_CONTEXT]


@pytest.mark.asyncio
async def test_an_answer_before_holding_evidence_is_corrected():
    replies = iter(['{"shard_summary":{"criteria":[]}}', '{"tool":"read_files","paths":["README.md"]}',
                    '{"shard_summary":{"criteria":[]}}'])
    seen: list[list[dict[str, str]]] = []

    async def chat(messages, **_kwargs):
        seen.append([dict(message) for message in messages])
        return next(replies)

    async def fetch(path):
        return '# readme\n'

    budget = AttemptBudget(max_calls=16, max_reads=16)
    inspection = SourceInspection(['README.md'], fetch, LIMITS, budget)
    inspection.name = 'scope_shard_author'
    result = await run_agent(AgentContext(budget, LIMITS, chat), inspection,
                             [{'role': 'system', 'content': 'author'}, {'role': 'user', 'content': '{}'}],
                             AgentTask('shard_summary', lambda data: data))
    assert result == {'shard_summary': {'criteria': []}}
    assert 'evidence_required' in seen[1][-1]['content']
    assert len({conversation[0]['content'] for conversation in seen}) == 1


def test_uninspected_review_citations_are_corrected_together_without_echoing_them():
    from coordinare.services.environment_scope import uninspected_sources_errors

    held = {'README.md': 'a' * 64, 'Gemfile': 'b' * 64}
    [error] = uninspected_sources_errors(['README.md', 'private/guess-1', 'Gemfile', 'private/guess-2'], held)
    assert error['invalid_source_indices'] == ['1', '3'] and error['inspected_files'] == ['Gemfile', 'README.md']
    assert error['location'] == ['scope_assessment', 'sources', '1'] and error['more_inspected_files'] == 0
    assert 'private/guess' not in json.dumps(error)
    assert uninspected_sources_errors(['README.md'], held) == []

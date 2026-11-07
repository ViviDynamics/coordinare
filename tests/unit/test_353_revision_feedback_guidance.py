"""Correction feedback states the broken rule so authors stop repeating it."""
from __future__ import annotations

import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.test_353_scope_contract import (
    Replies,
    closure,
    criterion,
    objection,
    proposal,
    revision,
    run,
)

READ_GUIDE = {'tool': 'read_file', 'path': 'guide'}


def errors(replies, index=-1):
    return json.loads(replies.messages[index][-1]['content'])['validation_errors']


def many(count):
    return [criterion(usability=f'Documented operation {i} succeeds') for i in range(count)]


@pytest.mark.asyncio
async def test_oversized_revision_feedback_states_limit_count_and_retention():
    from coordinare.services.environment_admission import REVISION_LIMIT_GUIDANCE
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(*many(38)),
                       revision(criterion(usability='One genuinely new operation')), closure()])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    error = next(item for item in errors(replies, -2) if item['code'] == 'too_long')
    assert error['limit'] == 16 and error['submitted'] == 38
    assert error['guidance'] == REVISION_LIMIT_GUIDANCE and 'retained' in REVISION_LIMIT_GUIDANCE
    assert budget.schema_repairs == 1 and budget.scope_revisions == 1
    assert len(scope.definition.criteria) == 2


@pytest.mark.asyncio
async def test_combined_limit_feedback():
    reassess = [{'criterion_id': f'S{i:016x}', 'criterion': criterion(usability=f'Reassessed {i}')} for i in range(10)]
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(*many(10), reassess=reassess),
                       revision(), closure()])
    budget = AttemptBudget()
    await run(replies, budget=budget)
    error = next(item for item in errors(replies, -2) if item.get('limit') == 16)
    assert error['submitted'] == 20


@pytest.mark.asyncio
async def test_malformed_source_path_gets_exact_path_guidance():
    from coordinare.services.environment_admission import SOURCE_PATH_GUIDANCE
    bad = {'scope': {'criteria': [dict(criterion(), sources=['./guide/'])]}, 'finding_responses': []}
    replies = Replies([READ_GUIDE, bad, proposal(criterion()), {'scope_assessment': {'verdict': 'accept',
        'rationale': 'Independent source judgment', 'sources': ['guide'], 'findings': [], 'decisions': []}}])
    budget = AttemptBudget()
    await run(replies, budget=budget)
    feedback = errors(replies, 2)
    assert any(item.get('guidance') == SOURCE_PATH_GUIDANCE for item in feedback)
    assert './guide/' not in json.dumps(feedback)
    assert budget.schema_repairs == 1

"""Malformed review citations use finite corrections without weakening evidence."""
from __future__ import annotations

import copy
import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import discover_and_review
from tests.unit.test_353_discovery_repair import (
    CAPABILITIES,
    COMMIT,
    PATH,
    SOURCE,
    Replies,
    assessment,
    candidate,
    run,
)


def invalid_review(verdict='revise'):
    result = assessment(candidate(), verdict)
    result['assessment']['sources'] = [PATH, 'manifest.checks', 'manifest.requirements',
        'manifest.probe_evidence.results (versions)']
    return result


@pytest.mark.asyncio
async def test_invalid_citations_correct_then_revision_gets_fresh_review():
    original = candidate()
    revised = copy.deepcopy(original)
    revised['checks'][0]['argv'].append('--strict')
    replies = Replies([{'tool':'read_file', 'path':PATH}, {'manifest':original},
        invalid_review(), assessment(original, 'revise'), {'manifest':revised}, assessment(revised)])
    budget = AttemptBudget()
    manifest, review = await run(replies, budget=budget)
    assert manifest.digest() == review.manifest_digest
    assert manifest.checks[0].argv[-1] == '--strict'
    assert budget.schema_repairs == 1
    assert budget.coverage_revisions == 1
    correction = json.loads(replies.messages[3][-1]['content'])
    assert correction['validation_errors'] == [{'location':['sources'], 'code':'review_citations_invalid',
        'allowed_sources':[PATH]}]
    feedback = json.loads(replies.messages[4][-1]['content'])
    assert feedback['coverage_revision']['sources'] == [PATH]
    assert len(replies.messages[5]) == 2
    assert not any(m['role'] == 'assistant' for m in replies.messages[5])


@pytest.mark.asyncio
async def test_invalid_acceptance_requires_another_actual_reviewer_reply():
    replies = Replies([{'tool':'read_file', 'path':PATH}, {'manifest':candidate()},
        invalid_review('accept'), assessment(candidate())])
    budget = AttemptBudget()
    _, review = await run(replies, budget=budget)
    assert review.sources == [PATH]
    assert len(replies.messages) == 4
    assert budget.schema_repairs == 1 and budget.coverage_revisions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('already_used', [0, 2])
async def test_repeated_bad_citations_cannot_exceed_existing_schema_budget(already_used):
    replies = Replies([{'tool':'read_file', 'path':PATH}, {'manifest':candidate()},
        *[invalid_review('accept')]*3])
    budget = AttemptBudget(schema_repairs=already_used)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget)
    assert failure.value.code == 'attempt_schema_limit'
    assert budget.schema_repairs == 2 and budget.coverage_revisions == 0
    assert len(replies.messages) == 2 + (3-already_used)


@pytest.mark.asyncio
async def test_correcting_citations_can_read_additional_pinned_evidence():
    valid = assessment(candidate())
    valid['assessment']['sources'].append('other/evidence')
    replies = Replies([{'tool':'read_file', 'path':PATH}, {'manifest':candidate()},
        invalid_review('accept'), {'tool':'read_file', 'path':'other/evidence'}, valid])
    budget = AttemptBudget()
    _, review = await run(replies, budget=budget)
    assert set(review.sources) == {PATH, 'other/evidence'}
    assert set(review.inputs) == {PATH, 'other/evidence'}
    assert budget.schema_repairs == 1


@pytest.mark.asyncio
async def test_source_drift_during_review_stays_terminal_without_correction():
    reads = 0
    async def fetch(path):
        nonlocal reads
        reads += 1
        return SOURCE if reads <= 2 else SOURCE + ' changed'
    replies = Replies([{'tool':'read_file', 'path':PATH}, {'manifest':candidate()},
        {'tool':'read_file', 'path':PATH}, assessment(candidate())])
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError, match='snapshot changed'):
        await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH],
            fetch=fetch, chat=replies, capabilities=CAPABILITIES, budget=budget)
    assert len(replies.messages) == 3
    assert budget.schema_repairs == 0 and budget.coverage_revisions == 0

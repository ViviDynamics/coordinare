"""Scope revision phases retain evidence without inheriting prior phase read counters."""
from __future__ import annotations

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_scope import discover_scope
from tests.unit.test_353_scope_contract import (
    COMMIT,
    SOURCES,
    Replies,
    criterion,
    proposal,
    review,
    revision,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('total_reads', [2, 1])
async def test_revision_has_own_phase_reads_but_shared_attempt_limit(total_reads):
    finding = {'category': 'coverage', 'issue': 'Nested service obligation missing',
               'sources': ['odd/nest'], 'affected_ids': []}
    replies = Replies([
        {'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        {'tool': 'read_file', 'path': 'odd/nest'},
        review('revise', findings=[finding], sources=list(SOURCES)),
        revision(criterion('odd/nest', usability='Service persists a request'), sources=['odd/nest']),
        review(decisions=[{'finding_id': 'F0001', 'status': 'resolved',
                          'rationale': 'Nested obligation retained', 'sources': ['odd/nest'],
                          'duplicate_of': None}], sources=list(SOURCES)),
    ])
    budget = AttemptBudget(max_reads=total_reads, max_coverage_revisions=0)

    async def fetch(path):
        return SOURCES.get(path)

    async def run():
        return await discover_scope(paths=list(SOURCES), fetch=fetch, chat=replies, commit=COMMIT,
                                    limits=DiscoveryLimits(max_reads=2), budget=budget)

    if total_reads == 1:
        with pytest.raises(EnvironmentDiscoveryError) as failure:
            await run()
        assert failure.value.code == 'attempt_read_limit'
        assert budget.scope_contract is None
        return
    scope = await run()
    assert len(scope.definition.criteria) == 2
    assert scope.finding_ledger.open_ids() == []
    assert scope.definition.inputs == scope.all_inputs
    assert set(scope.all_inputs) == set(SOURCES)
    # Only the model's two reads are charged; the daemon's fresh per-role verification is counted apart.
    assert budget.reads == 2 and budget.verification_reads == 4
    assert budget.scope_revisions == 1
    assert budget.coverage_revisions == 0
    assert len(scope.finding_ledger.reviews) == 2

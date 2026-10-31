"""Source revisions cannot outrun explicitly delegated pinned inspections."""
from __future__ import annotations

import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_353_scope_contract import (
    COMMIT,
    Replies,
    criterion,
    proposal,
    review,
    revision,
)


def objection(requests):
    return {'category': 'coverage', 'issue': 'Investigate nested source before revising',
            'sources': ['guide'], 'affected_ids': [], 'inspection_requests': requests}


async def discover(values, sources, *, fetch=None, budget=None):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_scope import discover_scope

    async def default_fetch(path):
        return sources.get(path)
    return await discover_scope(paths=list(sources), fetch=fetch or default_fetch, chat=Replies(values),
                                commit=COMMIT, limits=DiscoveryLimits(), budget=budget or AttemptBudget())


@pytest.mark.asyncio
async def test_nested_request_is_read_before_revision():
    sources = {'guide': 'See unfamiliar/deep/entry', 'unfamiliar/deep/entry': 'Required persistent operation'}
    events = []
    async def fetch(path):
        events.append(('fetch', path))
        return sources[path]
    def revise(messages):
        context = json.loads(messages[1]['content'])
        assert ('fetch', 'unfamiliar/deep/entry') in events
        assert context['investigation_evidence'][0]['result']['status'] == 'file'
        assert context['investigation_evidence'][0]['evidence']['content'] == sources['unfamiliar/deep/entry']
        return revision(criterion('unfamiliar/deep/entry'), sources=['unfamiliar/deep/entry'])(messages)
    values = [{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([{'operation': 'read_file', 'path': 'unfamiliar/deep/entry'}])]), revise,
        review(sources=list(sources), decisions=[{'finding_id': 'F0001', 'status': 'resolved',
              'rationale': 'Nested obligation retained', 'sources': ['unfamiliar/deep/entry'], 'duplicate_of': None}])]
    scope = await discover(values, sources, fetch=fetch)
    assert scope.finding_ledger.open_ids() == []
    assert 'unfamiliar/deep/entry' in scope.definition.inputs


@pytest.mark.asyncio
async def test_old_file_read_does_not_satisfy_pending_request():
    sources = {'guide': 'See nested', 'nested': 'Required operation'}
    observed = []
    async def fetch(path):
        observed.append(path)
        return sources[path]
    def premature(messages):
        assert observed.count('nested') == 1
        return revision(sources=['guide'])(messages)
    def repaired(messages):
        errors = json.loads(messages[-1]['content'])['validation_errors']
        assert errors[0]['code'] == 'scope_investigation_response_invalid'
        return revision(criterion('nested'), sources=['nested'])(messages)
    budget = AttemptBudget()
    values = [{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([{'operation': 'read_file', 'path': 'nested'}])]),
        {'tool': 'read_file', 'path': 'guide'}, premature, repaired,
        review(sources=list(sources), decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'Nested obligation retained', 'sources': ['nested'], 'duplicate_of': None}])]
    await discover(values, sources, fetch=fetch, budget=budget)
    assert budget.schema_repairs == 1 and budget.scope_revisions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('existing', [False, True])
async def test_missing_file_differs_from_failed_fetch(existing):
    sources = {'guide': 'Inspect requested nested path'}
    if existing:
        sources['nested'] = 'unavailable'
    async def fetch(path):
        return sources[path] if path == 'guide' else None
    def revised(messages):
        evidence = json.loads(messages[1]['content'])['investigation_evidence']
        assert evidence[0]['result']['status'] == 'absent'
        return revision(sources=['guide'])(messages)
    values = [{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([{'operation': 'read_file', 'path': 'nested'}])]), revised,
        review('indeterminate', decisions=[{'finding_id': 'F0001', 'status': 'open',
            'rationale': 'Absence does not resolve source conflict', 'sources': ['guide'], 'duplicate_of': None}])]
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await discover(values, sources, fetch=fetch, budget=budget)
    assert held.value.code == ('scope_investigation_unavailable' if existing else 'scope_review_indeterminate')
    assert budget.scope_findings.open_ids() == ['F0001'] and budget.scope_contract is None


@pytest.mark.asyncio
async def test_listing_page_does_not_satisfy_file_or_next_page():
    from coordinare_service_inference.environment.scope_investigation import ScopeInspectionBinding

    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_inspection import SourceInspection
    from coordinare.services.environment_scope_investigation import investigate_scope_requests

    sources = {f'nested/{i:03}': 'File content' for i in range(201)}
    reads = []
    async def fetch(path):
        reads.append(path)
        return sources[path]
    budget = AttemptBudget()
    inspection = SourceInspection(list(sources), fetch, DiscoveryLimits(), budget)
    bindings = [ScopeInspectionBinding.bind(finding_id='F0001', review_sequence=1, scope_digest='1'*64,
        source_commit=COMMIT, inventory_digest=inspection.inventory_digest, request=value) for value in [
            {'operation': 'list_files', 'path': 'nested', 'cursor': 0},
            {'operation': 'list_files', 'path': 'nested', 'cursor': 200},
            {'operation': 'read_file', 'path': 'nested/200'}]]
    results = await investigate_scope_requests(inspection, bindings)
    assert [result.status for result in results] == ['listing', 'listing', 'file']
    assert len({result.binding_id for result in results}) == 3
    assert results[0].evidence_digest != results[1].evidence_digest
    assert reads == ['nested/200'] and budget.reads == 1


@pytest.mark.asyncio
async def test_investigation_cannot_renew_attempt_deadline_or_submit_revision():
    import asyncio
    import time

    budget = AttemptBudget(timeout_seconds=0.05)
    sources = {'guide': 'Inspect nested source', 'nested': 'Required operation'}
    async def fetch(path):
        if path == 'nested':
            await asyncio.sleep(1)
        return sources[path]
    def forbidden(messages):
        pytest.fail('author revision ran before investigation finished')
    values = [{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([{'operation': 'read_file', 'path': 'nested'}])]), forbidden]
    started = time.monotonic()
    deadline = budget.deadline
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await discover(values, sources, fetch=fetch, budget=budget)
    assert held.value.code == 'attempt_deadline'
    assert time.monotonic()-started < 0.5
    assert budget.deadline == deadline and budget.scope_revisions == 1
    assert budget.scope_contract is None and budget.scope_findings.open_ids() == ['F0001']
    assert budget.scope_investigation_results == []

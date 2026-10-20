"""Author evidence reuse saves reads without sharing an independent review's fetches."""
from __future__ import annotations

import hashlib
from collections import Counter

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from tests.unit.test_353_scope_contract import (
    COMMIT,
    Replies,
    criterion,
    proposal,
    review,
    revision,
)


@pytest.mark.asyncio
async def test_56_author_inputs_are_not_refetched_each_revision():
    from coordinare.services.environment_scope import discover_scope

    sources = {'guide': 'Complete source obligations', **{f'odd/{i:02}': 'Source constraint' for i in range(55)}}
    paths = sorted(sources)
    fetched = Counter()
    async def fetch(path):
        fetched[path] += 1
        return sources[path]
    def revise(messages):
        import json
        evidence = json.loads(messages[1]['content'])['mandatory_evidence']
        assert {item['path']: item['content'] for item in evidence} == sources
        return revision(sources=['guide'])(messages)
    values = [*({'tool': 'read_files', 'paths': paths[i:i+8]} for i in range(0, 56, 8)), proposal(criterion()),
        review('revise', findings=[{'category': 'coverage', 'issue': 'Defend complete coverage with source',
            'sources': ['guide'], 'affected_ids': []}]), revise,
        review(decisions=[{'finding_id': 'F0001', 'status': 'resolved', 'rationale': 'Whole source checked',
            'sources': ['guide'], 'duplicate_of': None}])]
    budget = AttemptBudget()
    scope = await discover_scope(paths=paths, fetch=fetch, chat=Replies(values), commit=COMMIT,
                                 limits=DiscoveryLimits(), budget=budget)
    assert fetched == Counter(dict.fromkeys(paths, 3)), 'one author and two fresh reviewer fetches per file'
    assert budget.reads == 168 and len(scope.all_inputs) == 56
    assert len(scope.finding_ledger.reviews) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['inventory', 'commit', 'hash', 'bytes', 'context'])
async def test_cache_hash_identity_and_context_drift_hold(mutation):
    from coordinare_service_inference.environment.findings import FindingLedger

    from coordinare.services.environment_evidence import AuthorEvidenceCache, check_evidence_context

    sources = {'guide': 'Pinned source content'}
    async def fetch(path):
        return sources[path]
    budget = AttemptBudget(scope_findings=FindingLedger(source_commit=COMMIT))
    initial = SourceInspection(list(sources), fetch, DiscoveryLimits(), budget)
    await initial.read_files(['guide'])
    cache = AuthorEvidenceCache(source_commit=COMMIT, inventory_digest=initial.inventory_digest, max_bytes=1024)
    cache.capture(initial)
    expected = dict(initial.inputs)
    paths = ['different'] if mutation == 'inventory' else list(sources)
    if mutation == 'commit':
        budget.scope_findings = FindingLedger(source_commit='b'*40)
    if mutation == 'hash':
        expected['guide'] = 'f'*64
    limits = DiscoveryLimits(max_source_bytes=1) if mutation == 'bytes' else DiscoveryLimits()
    author = SourceInspection(paths, fetch, limits, budget)
    reads = budget.reads
    with pytest.raises(EnvironmentDiscoveryError):
        evidence = cache.restore(author, expected)
        if mutation == 'context':
            check_evidence_context([{'role': 'user', 'content': evidence[0]['content']}], max_bytes=1, phase='scope_revision')
    assert budget.reads == reads


@pytest.mark.asyncio
async def test_cache_miss_uses_remaining_read_budget():
    from coordinare_service_inference.environment.findings import FindingLedger

    from coordinare.services.environment_evidence import AuthorEvidenceCache

    sources = {'guide': 'Old content', 'nested': 'New required content'}
    fetched = []
    async def fetch(path):
        fetched.append(path)
        return sources[path]
    budget = AttemptBudget(max_reads=1, scope_findings=FindingLedger(source_commit=COMMIT))
    author = SourceInspection(list(sources), fetch, DiscoveryLimits(), budget)
    await author.read_files(['guide'])
    cache = AuthorEvidenceCache(source_commit=COMMIT, inventory_digest=author.inventory_digest, max_bytes=1024)
    cache.capture(author)
    fresh = SourceInspection(list(sources), fetch, DiscoveryLimits(), budget)
    expected = {path: hashlib.sha256(content.encode()).hexdigest() for path, content in sources.items()}
    restored = cache.restore(fresh, expected)
    assert [item['path'] for item in restored] == ['guide']
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await fresh.read_files(['nested'])
    assert held.value.code == 'attempt_read_limit'
    assert fetched == ['guide'] and 'nested' not in fresh.inputs

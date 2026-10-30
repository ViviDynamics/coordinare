"""Interrupted source investigations retain origin, findings and finite spent budgets."""
from __future__ import annotations

import asyncio
import copy

import pytest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import begin_attempt, save_attempt
from tests.unit.test_353_execution_repair import cache_fixture
from tests.unit.test_353_scope_contract import (
    COMMIT,
    Replies,
    criterion,
    proposal,
    review,
    revision,
)
from tests.unit.test_353_scope_investigation_flow import objection

SOURCES = {'guide': 'See nested source', 'nested': 'Required persistent operation'}


class Interrupted(BaseException):
    pass


async def saved_investigation(tmp_path, boundary='queue'):
    from coordinare.services.environment_scope import discover_scope

    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    async def fetch(path):
        if path == 'nested' and boundary == 'fetch':
            raise asyncio.CancelledError
        return SOURCES[path]
    async def checkpoint():
        if boundary == 'before_result_checkpoint' and budget.scope_investigation_results:
            raise asyncio.CancelledError
        save_attempt(cache, budget)
        if boundary == 'queue' and budget.scope_phase == 'investigation':
            raise Interrupted
        if boundary == 'result' and budget.scope_investigation_results:
            raise Interrupted
        if boundary == 'revision' and budget.scope_phase == 'revision':
            raise Interrupted
    budget.checkpoint = checkpoint
    values = [{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([{'operation': 'read_file', 'path': 'nested'}])])]
    with pytest.raises((Interrupted, asyncio.CancelledError)):
        await discover_scope(paths=list(SOURCES), fetch=fetch, chat=Replies(values), commit=COMMIT,
                             limits=DiscoveryLimits(), budget=budget)
    return cache


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['queue', 'fetch', 'before_result_checkpoint', 'result', 'revision'])
async def test_recovery_preserves_queue_findings_and_spent_budget(tmp_path, boundary):
    from coordinare.services.environment_scope import discover_scope

    cache = await saved_investigation(tmp_path, boundary)
    saved = copy.deepcopy(cache.environment_attempt)
    assert saved['version'] == 9 and saved['scope_author_protocol'] == 4
    assert len(saved['scope_investigations']) == 1
    assert len(saved['scope_investigation_results']) == int(boundary in {'result', 'revision'})
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    assert resumed.reads == saved['reads'] and resumed.calls == saved['calls']
    assert resumed.verification_reads == saved['verification_reads']
    assert resumed.scope_revisions == saved['scope_revisions'] == 1
    async def checkpoint():
        save_attempt(cache, resumed)
    resumed.checkpoint = checkpoint
    async def fetch(path):
        return SOURCES[path]
    values = [revision(criterion('nested'), sources=['nested']),
        review(sources=list(SOURCES), decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'Nested source retained', 'sources': ['nested'], 'duplicate_of': None}])]
    scope = await discover_scope(paths=list(SOURCES), fetch=fetch, chat=Replies(values), commit=COMMIT,
                                 limits=DiscoveryLimits(), budget=resumed)
    assert scope.finding_ledger.open_ids() == []
    assert cache.environment_attempt['started_at'] == saved['started_at']
    assert cache.environment_attempt['expires_at'] == saved['expires_at']
    assert resumed.reads >= saved['reads'] and resumed.scope_revisions == 1
    assert resumed.verification_reads > saved['verification_reads']
    assert cache.environment_attempt['scope_investigations'] == saved['scope_investigations']
    assert len(cache.environment_attempt['scope_investigation_results']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['protocol', 'review', 'request_id', 'commit', 'inventory'])
async def test_unknown_or_rebound_queue_holds(tmp_path, mutation):
    from coordinare_service_inference.environment.scope_investigation import ScopeInspectionBinding

    cache = await saved_investigation(tmp_path)
    if mutation == 'protocol':
        cache.environment_attempt['scope_author_protocol'] = 99
    else:
        data = cache.environment_attempt['scope_investigations'][0]
        if mutation == 'request_id':
            data['request']['path'] = 'other'
        else:
            key, value = {'review': ('review_sequence', 2), 'commit': ('source_commit', 'b'*40),
                          'inventory': ('inventory_digest', 'f'*64)}[mutation]
            data[key] = value
            cache.environment_attempt['scope_investigations'][0] = ScopeInspectionBinding.bind(
                **{key: value for key, value in data.items() if key != 'id'}).model_dump(mode='json')
    saved = copy.deepcopy(cache.environment_attempt)
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    assert cache.environment_attempt == saved


@pytest.mark.asyncio
async def test_completed_binding_survives_later_scope_revision(tmp_path):
    from coordinare.services.environment_scope import discover_scope

    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    async def checkpoint():
        save_attempt(cache, budget)
    budget.checkpoint = checkpoint
    async def fetch(path):
        return SOURCES[path]
    decision = {'finding_id': 'F0001', 'status': 'open', 'rationale': 'Recheck source completeness',
                'sources': ['nested'], 'duplicate_of': None}
    values = [{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([{'operation': 'read_file', 'path': 'nested'}])]),
        revision(criterion('nested'), sources=['nested']), review('revise', sources=list(SOURCES), decisions=[decision]),
        revision(sources=['nested']), review(sources=list(SOURCES), decisions=[{**decision, 'status': 'resolved'}])]
    scope = await discover_scope(paths=list(SOURCES), fetch=fetch, chat=Replies(values), commit=COMMIT,
                                 limits=DiscoveryLimits(), budget=budget)
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    binding = resumed.scope_investigations[0]
    assert binding.review_sequence == 1
    assert binding.scope_digest == scope.finding_ledger.reviews[0].manifest_digest
    assert binding.scope_digest != scope.definition.digest()
    assert len(resumed.scope_investigation_results) == 1 and resumed.scope_revisions == 2


@pytest.mark.asyncio
async def test_missing_memory_cache_is_reverified_without_model_reads(tmp_path):
    from coordinare.services.environment_scope import discover_scope

    cache = await saved_investigation(tmp_path, 'result')
    cache.environment_attempt['max_reads'] = cache.environment_attempt['reads']
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    verified = resumed.verification_reads
    fetched = []
    async def fetch(path):
        fetched.append(path)
        return SOURCES[path]
    values = [revision(criterion('nested'), sources=['nested']),
        review(sources=list(SOURCES), decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'Nested source retained', 'sources': ['nested'], 'duplicate_of': None}])]
    scope = await discover_scope(paths=list(SOURCES), fetch=fetch, chat=Replies(values), commit=COMMIT,
                                 limits=DiscoveryLimits(), budget=resumed)
    # The daemon re-verifies the lost content by digest; the model's exhausted read cap is never charged.
    assert scope.finding_ledger.open_ids() == []
    assert set(fetched) == set(SOURCES)
    assert resumed.reads == resumed.max_reads and resumed.verification_reads > verified


@pytest.mark.asyncio
async def test_legacy_held_attempt_stays_terminal(tmp_path):
    cache = await saved_investigation(tmp_path)
    cache.environment_attempt.update(version=7, scope_author_protocol=2, status='held')
    cache.environment_attempt.pop('scope_investigations', None)
    cache.environment_attempt.pop('scope_investigation_results', None)
    saved = copy.deepcopy(cache.environment_attempt)
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    assert cache.environment_attempt == saved


@pytest.mark.asyncio
@pytest.mark.parametrize(('verdict', 'maximum', 'code'), [
    ('revise', 0, 'attempt_scope_revision_limit'), ('indeterminate', 2, 'scope_review_indeterminate')])
async def test_terminal_review_persists_new_findings_and_pending_queue_as_held(tmp_path, verdict, maximum, code):
    from coordinare.services.environment_scope import discover_scope

    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    limits = DiscoveryLimits(max_scope_revisions=maximum)
    budget = begin_attempt(cache, limits, outer_seconds=600)
    async def checkpoint():
        save_attempt(cache, budget)
    budget.checkpoint = checkpoint
    async def fetch(path):
        return SOURCES[path]
    values = [{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review(verdict, findings=[objection([{'operation': 'read_file', 'path': 'nested'}])])]
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await discover_scope(paths=list(SOURCES), fetch=fetch, chat=Replies(values), commit=COMMIT,
                             limits=limits, budget=budget)
    assert held.value.code == code
    saved = cache.environment_attempt
    assert saved['status'] == 'held' and saved['last_error'] == code
    assert len(saved['scope_investigations']) == 1 and saved['scope_investigation_results'] == []
    assert len(saved['scope_findings']['records']) == 1 and saved['coverage_revisions'] == 0
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)


@pytest.mark.asyncio
async def test_recovery_rechecks_completed_result_digest_before_author(tmp_path):
    from coordinare.services.environment_scope import discover_scope

    cache = await saved_investigation(tmp_path, 'result')
    cache.environment_attempt['scope_investigation_results'][0]['evidence_digest'] = 'f'*64
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    before = resumed.calls
    async def fetch(path):
        return SOURCES[path]
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await discover_scope(paths=list(SOURCES), fetch=fetch, chat=Replies([]), commit=COMMIT,
                             limits=DiscoveryLimits(), budget=resumed)
    assert held.value.code == 'scope_investigation_identity'
    assert resumed.calls == before and resumed.scope_contract is None


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['scope_investigations', 'scope_investigation_results'])
async def test_checkpoint_cannot_remove_investigation_history(tmp_path, field):
    cache = await saved_investigation(tmp_path, 'result')
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    original = copy.deepcopy(cache.environment_attempt)
    setattr(resumed, field, [])
    with pytest.raises(EnvironmentDiscoveryError) as held:
        save_attempt(cache, resumed)
    assert held.value.code == 'scope_investigation_identity'
    assert cache.environment_attempt == original

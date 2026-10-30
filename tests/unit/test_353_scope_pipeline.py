"""Scope checkpoints retain independent findings, original expiry and consumed work."""
from __future__ import annotations

import copy
import time

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import (
    begin_attempt,
    restart_environment_attempt,
    save_attempt,
)
from tests.unit.test_353_execution_repair import cache_fixture
from tests.unit.test_353_scope_contract import COMMIT, Replies, criterion, proposal, review, run


@pytest.mark.asyncio
async def test_scope_contract_roundtrips_without_new_budget_or_mutable_alias(tmp_path):
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    now = time.time()
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=now)
    budget.checkpoint = lambda: checkpoint(cache, budget)
    contract = await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()]), budget=budget)
    original_expiry = cache.environment_attempt['expires_at']
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=now+1)
    assert resumed.scope_contract is not None
    assert resumed.scope_contract.digest() == contract.digest()
    assert resumed.calls == budget.calls and resumed.reads == budget.reads
    assert cache.environment_attempt['expires_at'] == original_expiry
    assert cache.environment_attempt['version'] == 9
    resumed.scope_contract.definition.criteria[0].usability = 'mutated in caller'
    assert cache.environment_attempt['scope_contract']['definition']['criteria'][0]['usability'] != 'mutated in caller'


async def checkpoint(cache, budget):
    save_attempt(cache, budget)


@pytest.mark.asyncio
async def test_saved_scope_cannot_be_removed_or_replaced_by_narrower_criteria(tmp_path):
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.checkpoint = lambda: checkpoint(cache, budget)
    await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()]), budget=budget)
    saved = copy.deepcopy(cache.environment_attempt)
    budget.scope_contract = None
    budget.scope_definition = None
    budget.scope_findings = None
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        save_attempt(cache, budget)
    assert failure.value.code == 'scope_obligation_removed'
    assert cache.environment_attempt == saved


def test_pre_scope_journal_cannot_resume_as_reviewed(tmp_path):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    cache.environment_attempt['version'] = 5
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    assert failure.value.code == 'legacy_attempt_requires_restart'
    restart_environment_attempt(cache, cleanup_confirmed=True)
    assert cache.environment_attempt is None
    assert cache.environment_attempt_archive[-1]['version'] == 5


@pytest.mark.asyncio
async def test_completed_scope_read_survives_crash_before_first_declaration(tmp_path):
    class Crash(BaseException):
        pass
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    async def saved():
        save_attempt(cache, budget)
        if getattr(budget, 'scope_inputs', {}):
            raise Crash
    budget.checkpoint = saved
    with pytest.raises(Crash):
        await run(Replies([{'tool': 'read_file', 'path': 'guide'}]), budget=budget)
    assert cache.environment_attempt['scope_inputs']['guide']
    assert cache.environment_attempt['scope_definition'] is None
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    assert resumed.scope_inputs == cache.environment_attempt['scope_inputs']


@pytest.mark.asyncio
async def test_production_discovery_reviews_scope_before_generating_commands():
    import json

    from coordinare.services.environment_discovery import discover_and_review
    from tests.unit.test_353_discovery_repair import CAPABILITIES, assessment
    from tests.unit.test_353_scope_contract import SOURCES

    phases = []
    async def fetch(path):
        return SOURCES.get(path)
    async def chat(messages, *, response_schema):
        system = messages[0]['content']
        context = json.loads(messages[1]['content'])
        if 'Author source-defined obligations' in system:
            phases.append('scope_author')
            if not any('tool_result' in message['content'] for message in messages[2:]):
                return json.dumps({'tool': 'read_file', 'path': 'guide'})
            return json.dumps(proposal(criterion()))
        if 'Independently review source-defined scope' in system:
            phases.append('scope_review')
            assert 'manifest' not in context and 'execution_capabilities' not in context
            return json.dumps(review())
        if 'manifest' in context:
            phases.append('manifest_review')
            assert context['manifest']['schema_version'] == 3
            value = assessment({})
            value['assessment']['sources'] = ['guide']
            return json.dumps(value)
        phases.append('construction')
        assert 'scope_contract' in context
        identifier = context['scope_contract']['definition']['criteria'][0]['id']
        manifest_schema = response_schema['$defs']['EnvironmentManifestProposal']
        assert 'criterion_checks' in manifest_schema['required']
        mapping = manifest_schema['properties']['criterion_checks']
        assert mapping['required'] == [identifier]
        assert set(mapping['properties']) == {identifier}
        assert mapping['additionalProperties'] is False
        return json.dumps({'manifest': {'criterion_checks': {identifier: ['operate']},
            'requirements': [{'id': 'source-operation', 'description': 'Documented operation', 'source': 'guide', 'checks': ['operate']}],
            'install': [], 'checks': [{'id': 'operate', 'argv': ['tool', 'exercise']}],
            'activation': {}, 'services': [], 'empty_reason': None, 'runtime_endpoints': []}, 'finding_responses': []})
    budget = AttemptBudget()
    manifest, assessed = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=list(SOURCES),
        fetch=fetch, chat=chat, capabilities=CAPABILITIES, budget=budget)
    assert phases == ['scope_author', 'scope_author', 'scope_review', 'construction', 'manifest_review']
    assert manifest.scope_contract_digest == budget.scope_contract.digest()
    assert assessed.manifest_digest == manifest.digest()
    assert budget.calls == 5 and budget.calls_completed == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(('phase', 'calls'), [('revision', 3), ('revision', 4), ('review', 4)])
async def test_scope_revision_crash_resumes_exact_phase_without_renewing_budget(tmp_path, phase, calls):
    from tests.unit.test_353_scope_contract import closure, objection, revision
    class Crash(BaseException):
        pass
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    expiry = cache.environment_attempt['expires_at']
    async def crash_checkpoint():
        save_attempt(cache, budget)
        if getattr(budget, 'scope_phase', None) == phase and budget.calls == calls:
            raise Crash
    budget.checkpoint = crash_checkpoint
    with pytest.raises(Crash):
        await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), objection(),
            revision(criterion(usability='Added operation')), closure()]), budget=budget)
    saved = copy.deepcopy(cache.environment_attempt)
    assert saved['version'] == 9 and saved['scope_author_protocol'] == 4
    assert saved['scope_phase'] == phase
    if phase == 'revision':
        assert saved['scope_revision_base_digest'] == budget.scope_definition.digest()
    else:
        assert saved['scope_revision_base_digest'] is None and len(saved['scope_definition']['criteria']) == 2
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    assert resumed.scope_phase == phase and resumed.calls == calls
    assert resumed.scope_responses == saved['scope_responses']
    assert resumed.scope_findings.model_dump(mode='json') == saved['scope_findings']
    assert cache.environment_attempt['expires_at'] == expiry
    resumed.checkpoint = lambda: checkpoint(cache, resumed)
    replies = Replies([revision(criterion(usability='Added operation')), closure()] if phase == 'revision' else [closure()])
    scope = await run(replies, budget=resumed)
    assert len(scope.definition.criteria) == 2 and len(scope.finding_ledger.reviews) == 2
    assert resumed.calls == calls + (2 if phase == 'revision' else 1)
    assert resumed.reads >= saved['reads'] and resumed.coverage_revisions == saved['coverage_revisions'] == 1
    assert resumed.schema_repairs == saved['schema_repairs'] == 0
    assert cache.environment_attempt['scope_phase'] == 'accepted'
    assert cache.environment_attempt['expires_at'] == expiry
    assert len(replies.messages[-1]) == 2


@pytest.mark.asyncio
async def test_scope_checkpoint_is_published_before_fresh_reviewer_call(tmp_path):
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.checkpoint = lambda: checkpoint(cache, budget)
    scripted = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()])
    async def chat(messages, *, response_schema):
        if 'Independently review source-defined scope' in messages[0]['content']:
            saved = cache.environment_attempt
            assert saved['scope_phase'] == 'review'
            assert saved['scope_definition'] == budget.scope_definition.model_dump(mode='json')
            assert saved['calls'] == budget.calls == 3
        return await scripted(messages, response_schema=response_schema)
    await run(chat, budget=budget)


@pytest.mark.asyncio
async def test_legacy_pending_source_journal_holds_and_explicit_restart_archives_consumption(tmp_path):
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.calls, budget.reads, budget.schema_repairs = 7, 9, 2
    save_attempt(cache, budget)
    cache.environment_attempt['version'] = 6
    for key in ('scope_author_protocol', 'scope_phase', 'scope_revision_base_digest'):
        cache.environment_attempt.pop(key, None)
    before = copy.deepcopy(cache.environment_attempt)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    assert failure.value.code == 'legacy_attempt_requires_restart'
    assert cache.environment_attempt == before
    restart_environment_attempt(cache, cleanup_confirmed=True)
    assert cache.environment_attempt_archive[-1] == before
    assert cache.environment_attempt is None


@pytest.mark.asyncio
async def test_legacy_accepted_scope_requires_restart_without_renewal(tmp_path):
    """Accepted scopes from earlier protocols lack a typed assessment and are never upgraded."""
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.checkpoint = lambda: checkpoint(cache, budget)
    await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()]), budget=budget)
    cache.environment_attempt['version'] = 6
    for key in ('scope_author_protocol', 'scope_phase', 'scope_revision_base_digest', 'scope_assessment'):
        cache.environment_attempt.pop(key, None)
    before = copy.deepcopy(cache.environment_attempt)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    assert failure.value.code == 'legacy_attempt_requires_restart'
    assert cache.environment_attempt == before


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing_protocol', 'wrong_base', 'phase', 'source'])
async def test_revision_checkpoint_tampering_is_rejected(tmp_path, case):
    from tests.unit.test_353_scope_contract import objection, revision
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.checkpoint = lambda: checkpoint(cache, budget)
    class Crash(BaseException):
        pass
    class CrashReplies(Replies):
        async def __call__(self, messages, *, response_schema):
            if 'scope_revision' in response_schema['anyOf'][-1].get('properties', {}):
                raise Crash
            return await super().__call__(messages, response_schema=response_schema)
    with pytest.raises(Crash):
        await run(CrashReplies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), objection(), revision()]), budget=budget)
    if case == 'missing_protocol':
        cache.environment_attempt.pop('scope_author_protocol', None)
    elif case == 'wrong_base':
        cache.environment_attempt['scope_revision_base_digest'] = '0'*64
    elif case == 'phase':
        cache.environment_attempt['scope_phase'] = 'initial'
    else:
        cache.environment_attempt['scope_inputs']['guide'] = '0'*64
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)


@pytest.mark.asyncio
async def test_held_source_findings_are_visible_without_exposing_private_evidence(tmp_path):
    import json

    from tests.unit.test_353_scope_contract import objection
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(max_coverage_revisions=0), outer_seconds=600)
    budget.checkpoint = lambda: checkpoint(cache, budget)
    with pytest.raises(EnvironmentDiscoveryError):
        await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), objection()]), budget=budget)
    save_attempt(cache, budget)
    summary = budget.summary()
    assert summary['finding_count'] == 0
    assert summary['scope_finding_count'] == 1 and summary['scope_open_finding_ids'] == ['F0001']
    assert summary['stage'] == 'scope_review' and summary['calls'] == 3 and summary['coverage_revisions'] == 0
    assert 0 < summary['remaining_seconds'] <= 600
    assert 'Documented coverage' not in json.dumps(summary) and 'rationale' not in json.dumps(summary)
    assert cache.environment_attempt['diagnostics']['scope_open_finding_ids'] == ['F0001']


@pytest.mark.asyncio
async def test_legacy_accepted_scope_migration_does_not_bypass_identity_validation(tmp_path):
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.checkpoint = lambda: checkpoint(cache, budget)
    await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()]), budget=budget)
    cache.environment_attempt['version'] = 6
    for key in ('scope_author_protocol', 'scope_phase', 'scope_revision_base_digest'):
        cache.environment_attempt.pop(key)
    cache.environment_attempt['scope_inputs']['guide'] = '0'*64
    before = copy.deepcopy(cache.environment_attempt)
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    assert cache.environment_attempt == before


@pytest.mark.asyncio
async def test_new_construction_source_reopens_fresh_review_with_durable_original_budget(tmp_path):
    import hashlib

    from coordinare.services.environment_construction import (
        ReviewedConstruction,
        ScopeReopenedError,
    )
    from tests.unit.test_353_discovery_repair import CAPABILITIES
    from tests.unit.test_353_scope_contract import SOURCES

    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.checkpoint = lambda: checkpoint(cache, budget)
    original = await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()]), budget=budget)
    expiry = cache.environment_attempt['expires_at']
    async def fetch(path):
        return SOURCES.get(path)
    chats = Replies([review(sources=list(SOURCES))])
    construction = ReviewedConstruction(('suite', COMMIT), list(SOURCES), fetch, chats, DiscoveryLimits(), budget, CAPABILITIES)
    with pytest.raises(ScopeReopenedError):
        await construction.observe_sources({'odd/nest': hashlib.sha256(SOURCES['odd/nest'].encode()).hexdigest()})
    assert budget.scope_contract is not None and budget.scope_phase == 'accepted'
    assert budget.scope_contract.digest() != original.digest()
    assert budget.scope_contract.definition == original.definition
    assert len(budget.scope_contract.finding_ledger.reviews) == 2
    assert 'odd/nest' in budget.scope_contract.all_inputs
    assert len(chats.messages) == 1 and 'Independently review source-defined scope' in chats.messages[0][0]['content']
    assert cache.environment_attempt['coverage_revisions'] == 1 and budget.coverage_revisions == 1
    assert cache.environment_attempt['expires_at'] == expiry
    assert cache.environment_attempt['scope_contract'] == budget.scope_contract.model_dump(mode='json')


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['manifest_author', 'manifest_review'])
async def test_reopened_source_review_crash_retains_consumption_and_resumes_without_author(tmp_path, stage):
    import hashlib

    from coordinare.services.environment_construction import ReviewedConstruction
    from tests.unit.test_353_discovery_repair import CAPABILITIES
    from tests.unit.test_353_scope_contract import SOURCES

    class Crash(BaseException):
        pass
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.checkpoint = lambda: checkpoint(cache, budget)
    await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()]), budget=budget)
    expiry = cache.environment_attempt['expires_at']
    async def fetch(path):
        return SOURCES.get(path)
    async def crash_checkpoint():
        save_attempt(cache, budget)
        if budget.scope_contract is None and budget.scope_phase == 'review':
            raise Crash
    budget.stage = stage
    budget.checkpoint = crash_checkpoint
    construction = ReviewedConstruction(('suite', COMMIT), list(SOURCES), fetch, Replies([]), DiscoveryLimits(), budget, CAPABILITIES)
    with pytest.raises(Crash):
        await construction.observe_sources({'odd/nest': hashlib.sha256(SOURCES['odd/nest'].encode()).hexdigest()})
    assert cache.environment_attempt['coverage_revisions'] == 1
    assert cache.environment_attempt['diagnostics']['stage'] == stage
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    resumed.checkpoint = lambda: checkpoint(cache, resumed)
    replies = Replies([review(sources=list(SOURCES))])
    construction.budget, construction.chat = resumed, replies
    await construction.prepare()
    assert len(replies.messages) == 1 and 'Independently review source-defined scope' in replies.messages[0][0]['content']
    assert resumed.coverage_revisions == 1 and resumed.calls == 4
    assert cache.environment_attempt['expires_at'] == expiry
    assert 'odd/nest' in resumed.scope_contract.all_inputs

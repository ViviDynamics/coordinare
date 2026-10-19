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
    assert cache.environment_attempt['version'] == 6
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

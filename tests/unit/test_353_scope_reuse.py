"""Reuse of an accepted scope by a later attempt, always through a fresh independent review (throughput
amendment, section 5)."""
from __future__ import annotations

import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_scope_reuse import reusable_scope
from tests.unit.test_353_shard_scope import COMMIT, SOURCES, Router, run


def fresh_budget() -> AttemptBudget:
    return AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)


async def accepted_journal() -> dict:
    budget = fresh_budget()
    contract = await run(Router(), budget=budget)
    return {'source_commit': COMMIT, 'scope_author_protocol': 4, 'started_at': 1.0,
            'scope_contract': json.loads(contract.model_dump_json()), 'scope_inputs': dict(budget.scope_inputs),
            'scope_shards': json.loads(json.dumps(budget.scope_shards))}


@pytest.mark.asyncio
async def test_a_reused_scope_skips_authoring_but_is_reviewed_afresh():
    journal = await accepted_journal()
    seed = reusable_scope([journal], COMMIT)
    assert seed is not None and 'reviewed_criteria' not in (seed['shards'] or {})
    router, budget = Router(), fresh_budget()
    budget.scope_reuse = seed
    contract = await run(router, budget=budget)
    roles = {role for role, _messages in router.conversations}
    assert 'author' not in roles and 'triage' not in roles
    assert router.reviews >= 1
    assert contract.definition.model_dump(mode='json') == seed['definition']
    assert budget.scope_reused_from == seed['from'] and budget.summary()['scope_reused_from'] == seed['from']
    assert budget.scope_reuse is None


@pytest.mark.asyncio
async def test_a_scope_for_another_commit_or_unaccepted_is_not_offered():
    journal = await accepted_journal()
    assert reusable_scope([journal], 'f' * 40) is None
    assert reusable_scope([{**journal, 'scope_author_protocol': 3}], COMMIT) is None
    assert reusable_scope([{**journal, 'scope_contract': None}], COMMIT) is None
    tampered = json.loads(json.dumps(journal))
    tampered['scope_contract']['source_assessment']['scope_digest'] = '0' * 64
    assert reusable_scope([tampered], COMMIT) is None


@pytest.mark.asyncio
async def test_a_changed_inventory_or_started_scope_is_not_adopted():
    from coordinare.services.environment_scope_reuse import adopt_reused_scope

    seed = reusable_scope([await accepted_journal()], COMMIT)
    changed = fresh_budget()
    changed.scope_reuse = seed
    adopt_reused_scope(changed, commit=COMMIT, paths=[*SOURCES, 'docs/new.md'])
    assert changed.scope_definition is None and changed.scope_phase == 'initial' and changed.scope_reuse is None
    started = fresh_budget()
    started.scope_reuse, started.scope_phase = seed, 'revision'
    adopt_reused_scope(started, commit=COMMIT, paths=list(SOURCES))
    assert started.scope_definition is None and started.scope_reused_from is None
    adopted = fresh_budget()
    adopted.scope_reuse = seed
    adopt_reused_scope(adopted, commit=COMMIT, paths=list(SOURCES))
    assert adopted.scope_phase == 'review' and adopted.scope_definition is not None


@pytest.mark.asyncio
async def test_a_new_attempt_after_restart_is_offered_the_archived_scope(tmp_path):
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture
    from tests.unit.test_353_shard_scope import LIMITS

    journal = await accepted_journal()
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    cache.environment_attempt_archive = [{'source_commit': COMMIT, 'scope_author_protocol': 4}, journal]
    budget = begin_attempt(cache, LIMITS, outer_seconds=600)
    assert budget.scope_reuse == reusable_scope([journal], COMMIT)
    save_attempt(cache, budget)
    assert begin_attempt(cache, LIMITS, outer_seconds=600).scope_reuse == budget.scope_reuse  # survives a restart
    other = cache_fixture(tmp_path / 'other')
    other.bootstrap_commit_sha = COMMIT
    other.environment_attempt_archive = [{'source_commit': COMMIT, 'scope_author_protocol': 4}]
    assert begin_attempt(other, LIMITS, outer_seconds=600).scope_reuse is None

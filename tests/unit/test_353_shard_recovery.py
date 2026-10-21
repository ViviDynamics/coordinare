"""Sharded discovery resumes from its journal without renewing budgets or repeating finished shards."""
from __future__ import annotations

import contextlib
import copy
import json

import pytest
from coordinare_service_inference.environment.scope import ScopeCriterionProposal, ShardSummary

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_repair import JOURNAL_MAX_BYTES, begin_attempt, save_attempt
from tests.unit.test_353_execution_repair import cache_fixture
from tests.unit.test_353_shard_scope import COMMIT, ENTRIES, LIMITS, PLAN, SOURCES, Router, criterion


class Interrupted(BaseException):
    pass


async def flow(cache, router, *, stop_when=None):
    from coordinare.services.environment_scope import discover_scope

    budget = begin_attempt(cache, LIMITS, outer_seconds=600)

    async def checkpoint():
        save_attempt(cache, budget)
        if stop_when is not None and stop_when(budget):
            raise Interrupted

    budget.checkpoint = checkpoint

    async def fetch(path):
        return SOURCES.get(path)
    with contextlib.suppress(Interrupted):
        await discover_scope(paths=list(SOURCES), fetch=fetch, chat=router, commit=COMMIT, limits=LIMITS,
                             budget=budget, entries=ENTRIES)
    return budget


def finished_shards(budget):
    return len((budget.scope_shards or {}).get('summaries', {}))


def fresh_cache(tmp_path):
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    return cache


@pytest.mark.asyncio
async def test_interrupted_shards_resume_without_repeating_finished_ones_or_renewal(tmp_path):
    cache = fresh_cache(tmp_path)
    first = Router()
    await flow(cache, first, stop_when=lambda budget: finished_shards(budget) >= 2)
    saved = copy.deepcopy(cache.environment_attempt)
    done = set(saved['scope_shards']['summaries'])
    assert len(done) >= 2 and saved['scope_phase'] == 'initial'
    second = Router()
    budget = await flow(cache, second)
    assert budget.scope_phase == 'accepted'
    resumed = {json.loads(messages[1]['content'])['shard']['id'] for role, messages in second.conversations if role == 'author'}
    assert not resumed & done and resumed | done >= {shard.id for shard in PLAN.shards}
    # Spent calls carry over; nothing is refunded or renewed.
    assert budget.calls > saved['calls'] and cache.environment_attempt['started_at'] == saved['started_at']


@pytest.mark.asyncio
async def test_restore_rejects_a_changed_shard_plan(tmp_path):
    cache = fresh_cache(tmp_path)
    await flow(cache, Router(), stop_when=lambda budget: finished_shards(budget) >= 1)
    cache.environment_attempt['scope_shards']['plan_digest'] = '0' * 64
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await flow(cache, Router())
    assert failure.value.code == 'scope_shard_identity'


@pytest.mark.asyncio
async def test_finished_summaries_are_cleared_once_the_definition_exists(tmp_path):
    cache = fresh_cache(tmp_path)
    await flow(cache, Router(), stop_when=lambda budget: budget.scope_phase == 'review')
    assert cache.environment_attempt['scope_shards']['summaries'] == {}
    assert cache.environment_attempt['scope_definition'] is not None


def test_journal_stays_within_16_mib_for_512_criteria():
    text = 'Documented obligation with observable output and cited rationale. ' * 6
    criteria = [ScopeCriterionProposal(**{**criterion(f'svc/{i}.txt', f'{i} {text}'), 'rationale': text[:2000]})
                for i in range(512)]
    summary = ShardSummary(shard_id='shard-000-00000000', criteria=criteria, cross_references=[], questions=[],
                           inputs={f'svc/{i}.txt': 'a' * 64 for i in range(512)})
    state = {'plan_digest': 'b' * 64, 'summaries': {summary.shard_id: summary.model_dump(mode='json')}}
    assert 3 * len(json.dumps(state).encode()) < JOURNAL_MAX_BYTES

"""Shard agents run in a bounded pool on the one shared attempt budget."""
from __future__ import annotations

import asyncio

import pytest

from coordinare.services.environment_agent_pool import ShardOverflow, run_pool
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError


def halves(item, overflow):
    remainder = overflow.remainder
    middle = len(remainder) // 2
    return [tuple(remainder[:middle]), tuple(remainder[middle:])]


@pytest.mark.asyncio
async def test_never_exceeds_concurrency():
    active = peak = 0

    async def worker(item):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return item

    results = await run_pool(list(range(40)), worker, budget=AttemptBudget(), concurrency=12, split=halves)
    assert peak == 12 and results == list(range(40))


@pytest.mark.asyncio
async def test_overflow_requeues_split_remainder():
    budget = AttemptBudget()
    seen = []

    async def worker(item):
        budget.reserve_call()
        seen.append(item)
        if len(item) > 2:
            raise ShardOverflow(item)
        return item

    results = await run_pool([('a', 'b', 'c', 'd'), ('e',)], worker, budget=budget, split=halves)
    assert results == [('a', 'b'), ('c', 'd'), ('e',)]
    # The overflowing call stays spent; retries are charged again, never refunded.
    assert budget.calls == len(seen) == 4


@pytest.mark.asyncio
async def test_budget_exhaustion_cancels_all_and_raises_once():
    budget = AttemptBudget(max_calls=3)
    cancelled = []

    async def worker(item):
        budget.reserve_call()
        try:
            await asyncio.sleep(10 if item else 0)
        except asyncio.CancelledError:
            cancelled.append(item)
            raise
        return item

    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run_pool([1, 2, 3, 0], worker, budget=budget, concurrency=4, split=halves)
    assert failure.value.code == 'attempt_call_limit'
    assert sorted(cancelled) == [1, 2, 3]


@pytest.mark.asyncio
async def test_results_order_is_deterministic():
    async def worker(item):
        await asyncio.sleep(0.001 * (10 - item))
        return item * 2

    assert await run_pool(list(range(10)), worker, budget=AttemptBudget(), split=halves) == [i * 2 for i in range(10)]


@pytest.mark.asyncio
async def test_no_new_allowances():
    budget = AttemptBudget(max_calls=5, max_reads=7)
    before = (budget.max_calls, budget.max_reads, budget.max_schema_repairs, budget.max_coverage_revisions, budget.deadline)

    async def worker(item):
        return item

    await run_pool([1, 2], worker, budget=budget, split=halves)
    assert (budget.max_calls, budget.max_reads, budget.max_schema_repairs, budget.max_coverage_revisions,
            budget.deadline) == before


@pytest.mark.asyncio
async def test_an_unsplittable_overflow_holds():
    async def worker(item):
        raise ShardOverflow(item)

    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run_pool([('only',)], worker, budget=AttemptBudget(), split=lambda item, exc: [item])
    assert failure.value.code == 'shard_context_limit'

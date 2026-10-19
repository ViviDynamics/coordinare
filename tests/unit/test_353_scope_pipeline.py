"""Scope checkpoints retain independent findings, original expiry and consumed work."""
from __future__ import annotations

import copy
import time

import pytest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
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

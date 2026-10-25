"""Scope and executable revisions have separate budgets, hold codes and journal counters."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from coordinare.config import EnvCacheConfig
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import begin_attempt, save_attempt
from tests.unit.test_353_execution_repair import cache_fixture


def test_scope_and_executable_revisions_are_counted_and_held_separately():
    budget = AttemptBudget(max_scope_revisions=2, max_coverage_revisions=1)
    budget.reserve_repair('scope')
    budget.reserve_repair('scope')
    budget.reserve_repair('coverage')
    assert (budget.scope_revisions, budget.coverage_revisions) == (2, 1)
    with pytest.raises(EnvironmentDiscoveryError) as scope_hold:
        budget.reserve_repair('scope')
    assert scope_hold.value.code == 'attempt_scope_revision_limit'
    with pytest.raises(EnvironmentDiscoveryError) as executable_hold:
        budget.reserve_repair('coverage')
    assert executable_hold.value.code == 'attempt_coverage_limit'
    assert (budget.scope_revisions, budget.coverage_revisions) == (2, 1)


def test_summary_reports_both_revision_counters_and_maxima():
    summary = AttemptBudget(max_scope_revisions=3, max_coverage_revisions=5).summary()
    assert (summary['scope_revisions'], summary['max_scope_revisions']) == (0, 3)
    assert (summary['coverage_revisions'], summary['max_coverage_revisions']) == (0, 5)
    assert summary['repairs'] == 0


def test_defaults_are_unchanged_unsharded_and_the_ruled_eight_when_sharded():
    assert AttemptBudget().max_scope_revisions == 2
    assert EnvCacheConfig().manifest_max_scope_revisions == EnvCacheConfig().manifest_max_coverage_revisions == 2
    sharded = EnvCacheConfig(manifest_shard_context_tokens=262144)
    assert (sharded.manifest_max_scope_revisions, sharded.manifest_max_coverage_revisions) == (8, 8)
    explicit = EnvCacheConfig(manifest_shard_context_tokens=262144, manifest_max_scope_revisions=3)
    assert (explicit.manifest_max_scope_revisions, explicit.manifest_max_coverage_revisions) == (3, 8)
    with pytest.raises(ValueError):
        EnvCacheConfig(manifest_max_scope_revisions=9)


def test_legacy_repair_alias_supplies_the_scope_limit_too():
    legacy = EnvCacheConfig(manifest_max_repairs=1)
    assert (legacy.manifest_max_scope_revisions, legacy.manifest_max_coverage_revisions) == (1, 1)
    with pytest.raises(ValueError):
        EnvCacheConfig(manifest_max_repairs=1, manifest_max_scope_revisions=2)


def test_discovery_limits_carry_the_scope_maximum(tmp_path):
    limits = DiscoveryLimits(max_scope_revisions=4, max_coverage_revisions=6)
    budget = begin_attempt(cache_fixture(tmp_path), limits, outer_seconds=3600, now=100)
    assert (budget.max_scope_revisions, budget.max_coverage_revisions) == (4, 6)


def test_journal_persists_both_counters_and_legacy_count_is_charged_to_both(tmp_path):
    limits = DiscoveryLimits(max_coverage_revisions=4)
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, limits, outer_seconds=3600, now=100)
    for _ in range(3):
        budget.reserve_repair('coverage')
    budget.reserve_repair('scope')
    save_attempt(cache, budget)
    assert (cache.environment_attempt['scope_revisions'], cache.environment_attempt['coverage_revisions']) == (1, 3)
    resumed = begin_attempt(cache, limits, outer_seconds=3600, now=110)
    assert (resumed.scope_revisions, resumed.coverage_revisions) == (1, 3)

    # A journal written before this change has one shared count. Neither counter may forget it.
    legacy = cache_fixture(tmp_path / 'legacy')
    budget = begin_attempt(legacy, limits, outer_seconds=3600, now=100)
    for _ in range(3):
        budget.reserve_repair('coverage')
    save_attempt(legacy, budget)
    for key in ('scope_revisions', 'max_scope_revisions'):
        legacy.environment_attempt.pop(key)
    wider = DiscoveryLimits(max_coverage_revisions=4, max_scope_revisions=4)
    resumed = begin_attempt(legacy, wider, outer_seconds=3600, now=110)
    assert (resumed.scope_revisions, resumed.coverage_revisions) == (3, 3)
    assert (resumed.max_scope_revisions, resumed.max_coverage_revisions) == (4, 4)


def test_a_scope_release_reopen_is_charged_to_the_scope_budget():
    from coordinare.services import environment_discovery as discovery

    budget = AttemptBudget(max_scope_revisions=0)
    budget.scope_contract = SimpleNamespace()
    attempt = SimpleNamespace(budget=budget, construction=SimpleNamespace(prepare=lambda: None))
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        asyncio.run(discovery._reopen_for_release(attempt, ['S1']))
    assert caught.value.code == 'attempt_scope_revision_limit'
    assert budget.scope_contract is not None and budget.release_requests == [] and budget.coverage_revisions == 0


def test_cli_accepts_a_scope_revision_flag(monkeypatch, capsys):
    import sys

    from scripts.validate_environment_repositories import main

    monkeypatch.setattr(sys, 'argv', ['validate', '--max-scope-revisions', '8', '--max-coverage-revisions', '8', '--help'])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    capsys.readouterr()

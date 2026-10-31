"""Each correction category has its own finite, validated allowance."""
from __future__ import annotations

import pytest

from coordinare.config import EnvCacheConfig
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError


def test_category_reservations_are_independent():
    budget = AttemptBudget(max_execution_repairs=2)
    for kind in ('schema', 'coverage', 'execution'):
        budget.reserve_repair(kind)
        budget.reserve_repair(kind)
    assert (budget.schema_repairs, budget.coverage_revisions, budget.execution_repairs) == (2, 2, 2)
    for kind in ('schema', 'coverage', 'execution'):
        with pytest.raises(EnvironmentDiscoveryError) as exc:
            budget.reserve_repair(kind)
        assert exc.value.code == f'attempt_{kind}_limit'
    assert budget.repairs == 6


@pytest.mark.parametrize(('kind', 'maximum'), [('schema', 'max_schema_repairs'),
    ('coverage', 'max_coverage_revisions'), ('execution', 'max_execution_repairs')])
def test_zero_category_limit_refuses_without_changing_counters(kind, maximum):
    budget = AttemptBudget(**{maximum: 0})
    with pytest.raises(EnvironmentDiscoveryError):
        budget.reserve_repair(kind)
    assert budget.repairs == 0


def test_category_config_defaults_legacy_alias_and_mixed_setting_rejection():
    defaults = EnvCacheConfig()
    assert defaults.manifest_max_schema_repairs == defaults.manifest_max_coverage_revisions == 2
    # Maintainer ruling 2026-10-31: four execution repairs.
    assert defaults.manifest_max_execution_repairs == 4
    legacy = EnvCacheConfig(manifest_max_repairs=0)
    assert legacy.manifest_max_schema_repairs == legacy.manifest_max_coverage_revisions == 0
    assert legacy.manifest_max_execution_repairs == 0
    with pytest.raises(ValueError, match='cannot combine'):
        EnvCacheConfig(manifest_max_repairs=1, manifest_max_schema_repairs=2)
    for name, invalid_upper in [('manifest_max_schema_repairs', 9), ('manifest_max_coverage_revisions', 9),
                                ('manifest_max_execution_repairs', 5)]:
        for invalid in (-1, invalid_upper):
            with pytest.raises(ValueError):
                EnvCacheConfig(**{name: invalid})


def test_larger_category_budgets_remain_finite_and_do_not_expand_execution():
    from coordinare.services.environment_discovery import DiscoveryLimits

    config = EnvCacheConfig(manifest_max_schema_repairs=8, manifest_max_coverage_revisions=8)
    limits = DiscoveryLimits(max_schema_repairs=config.manifest_max_schema_repairs,
        max_coverage_revisions=config.manifest_max_coverage_revisions)
    budget = AttemptBudget(max_schema_repairs=limits.max_schema_repairs,
        max_coverage_revisions=limits.max_coverage_revisions)
    for kind, count in [('schema', 8), ('coverage', 8), ('execution', 4)]:
        for _ in range(count):
            budget.reserve_repair(kind)
        with pytest.raises(EnvironmentDiscoveryError) as exc:
            budget.reserve_repair(kind)
        assert exc.value.code == f'attempt_{kind}_limit'
    assert (budget.schema_repairs, budget.coverage_revisions, budget.execution_repairs) == (8, 8, 4)
    assert EnvCacheConfig().manifest_max_coverage_revisions == 2


def test_resumed_attempt_never_renews_or_enlarges_original_allowances(tmp_path):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture

    cache = cache_fixture(tmp_path)
    limits = DiscoveryLimits(max_schema_repairs=8, max_coverage_revisions=8)
    budget = begin_attempt(cache, limits, outer_seconds=3600, now=100)
    for _ in range(3):
        budget.reserve_repair('schema')
    budget.reserve_repair('coverage')
    save_attempt(cache, budget)
    expires = cache.environment_attempt['expires_at']
    resumed = begin_attempt(cache, limits, outer_seconds=3600, now=110)
    assert (resumed.schema_repairs, resumed.coverage_revisions) == (3, 1)
    assert (resumed.max_schema_repairs, resumed.max_coverage_revisions) == (8, 8)
    smaller = DiscoveryLimits(max_schema_repairs=5, max_coverage_revisions=3)
    lowered = begin_attempt(cache, smaller, outer_seconds=3600, now=120)
    save_attempt(cache, lowered)
    resumed = begin_attempt(cache, limits, outer_seconds=3600, now=130)
    assert (resumed.max_schema_repairs, resumed.max_coverage_revisions) == (5, 3)
    assert cache.environment_attempt['expires_at'] == expires


def test_cli_accepts_explicit_larger_budgets(monkeypatch, capsys):
    import sys

    from scripts.validate_environment_repositories import main

    monkeypatch.setattr(sys, 'argv', ['validate', '--max-schema-repairs', '8',
        '--max-coverage-revisions', '8', '--help'])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    capsys.readouterr()

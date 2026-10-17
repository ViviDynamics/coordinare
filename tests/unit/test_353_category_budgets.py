"""Each correction category has its own finite, validated allowance."""
from __future__ import annotations

import pytest

from coordinare.config import EnvCacheConfig
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError


def test_category_reservations_are_independent():
    budget = AttemptBudget()
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
    assert defaults.manifest_max_execution_repairs == 2
    legacy = EnvCacheConfig(manifest_max_repairs=0)
    assert legacy.manifest_max_schema_repairs == legacy.manifest_max_coverage_revisions == 0
    assert legacy.manifest_max_execution_repairs == 0
    with pytest.raises(ValueError, match='cannot combine'):
        EnvCacheConfig(manifest_max_repairs=1, manifest_max_schema_repairs=2)
    for name in ('manifest_max_schema_repairs', 'manifest_max_coverage_revisions', 'manifest_max_execution_repairs'):
        for invalid in (-1, 3):
            with pytest.raises(ValueError):
                EnvCacheConfig(**{name: invalid})

"""Coverage findings cannot vanish on repair/restart or legacy migration."""
from __future__ import annotations

import copy

import pytest
from coordinare_service_inference.environment.findings import FindingLedger

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import (
    begin_attempt,
    restart_environment_attempt,
    save_attempt,
)
from tests.unit.test_353_execution_repair import cache_fixture
from tests.unit.test_353_review_findings_contract import ledger_data


def test_restart_preserves_findings_expiry_and_consumed_budgets(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    data = ledger_data()
    data['source_commit'] = cache.bootstrap_commit_sha
    data['records'][0]['source_commit'] = cache.bootstrap_commit_sha
    ledger = FindingLedger.model_validate(data)
    budget.finding_ledger = ledger
    budget.calls, budget.reads = 9, 20
    budget.schema_repairs, budget.coverage_revisions = 1, 2
    save_attempt(cache, budget)
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=200)
    assert resumed.finding_ledger.digest() == ledger.digest()
    assert (resumed.calls, resumed.reads, resumed.schema_repairs, resumed.coverage_revisions) == (9, 20, 1, 2)
    assert cache.environment_attempt['expires_at'] == 700
    assert cache.environment_attempt['version'] == 5
    resumed.finding_ledger.records[0].proposal.issue = 'Mutation'
    assert cache.environment_attempt['finding_ledger']['records'][0]['proposal']['issue'] != 'Mutation'


@pytest.mark.parametrize('version', [1, 2, 3, 4])
def test_legacy_attempt_requires_explicit_cleanup_restart(tmp_path, version):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    data = cache.environment_attempt
    data.pop('finding_ledger', None)
    data['version'] = version
    if version == 1:
        data['repairs'], data['max_repairs'] = 1, 2
        for name in ('schema_repairs', 'coverage_revisions', 'execution_repairs'):
            data.pop(name)
            data.pop('max_' + name)
    previous = copy.deepcopy(data)
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=200)
    assert exc.value.code == 'legacy_attempt_requires_restart'
    restart_environment_attempt(cache, cleanup_confirmed=True)
    assert cache.environment_attempt is None
    assert cache.environment_attempt_archive[-1] == previous


def test_ledger_cannot_be_removed_or_rewritten_at_checkpoint(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    data = ledger_data()
    data['source_commit'] = cache.bootstrap_commit_sha
    data['records'][0]['source_commit'] = cache.bootstrap_commit_sha
    budget.finding_ledger = FindingLedger.model_validate(data)
    save_attempt(cache, budget)
    budget.finding_ledger = None
    with pytest.raises(EnvironmentDiscoveryError):
        save_attempt(cache, budget)

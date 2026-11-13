"""The pending repair's triage is stored on the journal before any author call, and a restart resumes the same one."""

from __future__ import annotations

from coordinare_service_inference.environment.models import EnvironmentManifest, VerificationReport

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import (
    begin_attempt,
    journal_for,
    prepare_repair,
    record_execution_failure,
    repair_triage_for,
    save_attempt,
)
from coordinare.services.environment_repair_triage import RepairTriage, triage_failure
from tests.unit.test_353_execution_repair import cache_fixture, failed_report


def failed_first_generation(tmp_path) -> EnvCacheState:
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    budget.calls, budget.reads = 4, 2
    save_attempt(cache, budget, executing=True)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    return cache


def test_prepare_repair_stores_the_triage_of_the_rejected_execution(tmp_path):
    cache = failed_first_generation(tmp_path)
    assert cache.environment_attempt['repair_triage'] is None

    prepare_repair(cache)

    stored = RepairTriage.model_validate(cache.environment_attempt['repair_triage'])
    rejected = cache.environment_attempt['rejected'][0]
    data = dict(rejected['report'])
    data.pop('passed', None)
    expected = triage_failure(_rejected_manifest(rejected), VerificationReport.model_validate(data))
    assert stored == expected
    assert stored.manifest_digest == rejected['manifest_digest']
    assert stored.roots == {'baseline': 'command'}
    assert cache.environment_attempt['status'] == 'discovering'


def test_a_budget_for_the_repair_carries_the_stored_triage(tmp_path):
    cache = failed_first_generation(tmp_path)
    prepare_repair(cache)

    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=120)

    assert budget.repair_triage == repair_triage_for(journal_for(cache))
    assert budget.repair_triage is not None and budget.repair_triage.digest


def test_a_journal_written_before_the_triage_field_derives_the_same_triage(tmp_path):
    cache = failed_first_generation(tmp_path)
    prepare_repair(cache)
    stored = repair_triage_for(journal_for(cache))
    del cache.environment_attempt['repair_triage']

    assert repair_triage_for(journal_for(cache)) == stored


def test_the_first_generation_has_no_repair_triage(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)

    assert budget.repair_triage is None


def _rejected_manifest(rejected):
    return EnvironmentManifest.model_validate(rejected['manifest'])

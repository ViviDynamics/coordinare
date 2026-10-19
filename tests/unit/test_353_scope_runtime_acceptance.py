"""Current readiness requires reviewed scope; historical records stay readable."""
from __future__ import annotations

import pytest
from agent.performer.tests.source_contract_fixture import current_review_fixture
from coordinare_service_inference.environment.models import (
    EnvironmentManifest,
    ExecutionCapabilities,
    validate_current_coverage,
)

from coordinare.services.environment_acceptance import (
    accepted_environment,
    saved_execution_accepted,
)
from tests.unit.test_353_execution_repair import cache_fixture
from tests.unit.test_353_scope_mapping import mapped_manifest


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_manifest_with_current_findings_cannot_restore_ready_cache(tmp_path, version):
    cache = cache_fixture(tmp_path)
    data = dict(cache.environment_manifest)
    for name in ('scope_contract', 'scope_contract_digest', 'criterion_checks'):
        data.pop(name, None)
    data.update(schema_version=version, policy_version='351-v1' if version == 1 else '353-v2')
    if version == 2:
        from coordinare_service_inference.environment.runtime import RuntimePolicy, RuntimeSpec

        from tests.unit.test_353_runtime_identity import policy_data

        data['runtime'] = RuntimeSpec(schema_version=1, policy=RuntimePolicy.model_validate(policy_data()), endpoints=[]).model_dump()
    manifest = EnvironmentManifest.model_validate(data)
    cache.environment_manifest = manifest.model_dump()
    cache.environment_assessment = current_review_fixture(manifest, {
        **cache.environment_assessment, 'manifest_digest': manifest.digest(),
    })
    cache.cache_dir_ready = True
    assert accepted_environment(cache) is None
    assert not saved_execution_accepted(cache)
    # Parsing/migration still retains historical identity and source evidence.
    assert EnvironmentManifest.model_validate(cache.environment_manifest).digest() == manifest.digest()


@pytest.mark.asyncio
async def test_mutated_in_memory_scope_cannot_bypass_admission_validation():
    from coordinare_service_inference.environment.models import CoverageAssessment

    data, identifier = await mapped_manifest()
    manifest = EnvironmentManifest.model_validate(data)
    manifest.criterion_checks[identifier] = ['unlisted']
    assessment = CoverageAssessment.model_validate(current_review_fixture(manifest, {
        'manifest_digest': manifest.digest(), 'source_commit': manifest.source_commit,
        'symphony_name': manifest.symphony_name, 'inventory_digest': manifest.inventory_digest,
        'capabilities_digest': manifest.capabilities_digest, 'inputs': manifest.inputs,
        'sources': list(manifest.inputs), 'verdict': 'accept', 'rationale': 'Synthetic protocol evidence',
    }))
    with pytest.raises(ValueError):
        validate_current_coverage(manifest, assessment)


@pytest.mark.parametrize('mutation', ['platform', 'capabilities', 'missing_capabilities'])
def test_clean_execution_requires_actual_target_capability_identity(tmp_path, mutation):
    import json

    from coordinare.services.environment_acceptance import execution_report
    from tests.unit.env_manifest_fixture import verification_output

    cache = cache_fixture(tmp_path)
    report = json.loads(verification_output(cache.environment_manifest))
    if mutation == 'platform':
        report['platform'] = 'different/platform'
    elif mutation == 'capabilities':
        cache.environment_capabilities['image_ref'] = 'other/image@sha256:' + 'f' * 64
    else:
        cache.environment_capabilities = None
    with pytest.raises(ValueError):
        execution_report(json.dumps(report), cache)


def test_saved_clean_evidence_cannot_claim_another_immutable_image(tmp_path):
    cache = cache_fixture(tmp_path)
    assert saved_execution_accepted(cache)
    cache.environment_verification['image_ref'] = 'other/image@sha256:' + 'f' * 64
    assert not saved_execution_accepted(cache)


@pytest.mark.parametrize('command_id', ['baseline', 'start', 'health', 'stop', 'init'])
@pytest.mark.parametrize('status', ['missing', 'fail', 'not_run', 'indeterminate', 'unknown', 'false_pass'])
def test_scope_review_never_waives_required_check_or_lifecycle_result(tmp_path, command_id, status):
    import json

    from coordinare.services.environment_acceptance import execution_report
    from tests.unit.env_manifest_fixture import reviewed_metadata, verification_output

    cache = cache_fixture(tmp_path)
    service = {'id': 'service', 'required': True,
        **{name: {'id': name, 'argv': ['true']} for name in ('start', 'health', 'stop', 'init')}}
    metadata = reviewed_metadata(services=[service], symphony_name=cache.symphony_name,
        capabilities=ExecutionCapabilities.model_validate(cache.environment_capabilities))
    cache.environment_manifest = metadata['environment_manifest']
    cache.environment_assessment = metadata['environment_assessment']
    report = json.loads(verification_output(cache.environment_manifest))
    if status == 'missing':
        report['results'] = [result for result in report['results'] if result['id'] != command_id]
    else:
        result = next(result for result in report['results'] if result['id'] == command_id)
        result.update(status='pass' if status == 'false_pass' else status,
            exit_code=1 if status in ('false_pass', 'fail') else None)
    if status in ('missing', 'unknown', 'false_pass'):
        with pytest.raises(ValueError):
            execution_report(json.dumps(report), cache)
    else:
        assert not execution_report(json.dumps(report), cache).passed


@pytest.mark.parametrize('phase', ['scope_review', 'runtime_setup', 'project_execution'])
def test_real_snapshot_restart_preserves_scope_deadline_and_spent_work(tmp_path, phase):
    from types import SimpleNamespace

    from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.runtime_manifest_fixture import runtime_cache

    cache = runtime_cache(tmp_path / 'cache')
    manifest = EnvironmentManifest.model_validate(cache.environment_manifest)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=100)
    budget.calls, budget.reads = 7, 9
    budget.schema_repairs = budget.coverage_revisions = budget.execution_repairs = 1
    budget.scope_phase = "accepted"
    budget.scope_contract = manifest.scope_contract.model_copy(deep=True)
    budget.scope_definition = budget.scope_contract.definition.model_copy(deep=True)
    budget.scope_inputs = dict(budget.scope_contract.all_inputs)
    budget.scope_findings = budget.scope_contract.finding_ledger.model_copy(deep=True)
    save_attempt(cache, budget, executing=phase != 'scope_review')
    snapshots = _persist_env_cache({'sym': cache})
    restored = EnvCacheState(symphony_name='sym', sanitised_name='sym', cache_dir=cache.cache_dir)
    _rehydrate_env_cache({'env_cache': {'sym': restored}}, SimpleNamespace(env_cache=snapshots))
    assert restored.environment_manifest == cache.environment_manifest
    journal = restored.environment_attempt
    assert journal['scope_contract'] == cache.environment_attempt['scope_contract']
    assert (journal['calls'], journal['reads'], journal['schema_repairs'],
            journal['coverage_revisions'], journal['execution_repairs']) == (7, 9, 1, 1, 1)
    assert journal['expires_at'] == 700
    assert not restored.cache_dir_ready
    if phase == 'scope_review':
        resumed = begin_attempt(restored, DiscoveryLimits(), outer_seconds=3600, now=120)
        assert resumed.scope_contract.digest() == manifest.scope_contract_digest
        assert restored.environment_attempt['expires_at'] == 700
    else:
        assert journal['status'] == 'held' and journal['last_error'] == 'cleanup_unconfirmed'
        with pytest.raises(EnvironmentDiscoveryError):
            begin_attempt(restored, DiscoveryLimits(), outer_seconds=3600, now=120)


def test_runtime_failure_retains_bound_receipt_and_failed_evidence_for_repair(tmp_path):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import (
        begin_attempt,
        record_execution_failure,
        save_attempt,
    )
    from tests.unit.runtime_manifest_fixture import runtime_cache, runtime_report

    cache = runtime_cache(tmp_path / 'cache')
    report = runtime_report(cache)
    cache.environment_runtime_artifacts = report['runtime_artifacts']
    report['results'][1].update(status='fail', exit_code=2)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=100)
    save_attempt(cache, budget, executing=True)
    assert record_execution_failure(cache, report, cleanup_confirmed=True, now=101)
    assert not cache.cache_dir_ready
    rejected = cache.environment_attempt['rejected'][0]
    assert rejected['report']['runtime_artifacts'] == report['runtime_artifacts']
    assert rejected['report']['runtime_cleanup'] is True
    assert rejected['report']['results'][1]['status'] == 'fail'


@pytest.mark.parametrize('mutation', ['none', 'cleanup', 'source', 'unknown', 'missing'])
def test_failed_installer_without_success_receipt_is_bounded_repair_evidence(tmp_path, mutation):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import (
        begin_attempt,
        record_execution_failure,
        save_attempt,
    )
    from tests.unit.runtime_manifest_fixture import runtime_cache, runtime_report

    cache = runtime_cache(tmp_path / 'cache')
    report = runtime_report(cache, phase='installer')
    report['runtime_artifacts'] = None
    report['results'][0].update(status='fail', exit_code=2)
    report['results'][1].update(status='not_run', exit_code=None)
    if mutation == 'cleanup':
        report['runtime_cleanup'] = False
    elif mutation == 'source':
        report['execution_source_digest'] = '0' * 64
    elif mutation == 'unknown':
        report['results'][0]['id'] = 'invented'
    elif mutation == 'missing':
        report['results'].pop()
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=100)
    save_attempt(cache, budget, executing=True)
    assert record_execution_failure(cache, report, cleanup_confirmed=True, now=101) is (mutation == 'none')
    assert not cache.cache_dir_ready
    journal = cache.environment_attempt
    assert journal['expires_at'] == 700
    assert journal['execution_repairs'] == (1 if mutation == 'none' else 0)
    if mutation == 'none':
        rejected = journal['rejected'][0]['report']
        assert rejected['runtime_artifacts'] is None
        assert [item['status'] for item in rejected['results']] == ['fail', 'not_run']
        assert cache.environment_runtime_artifacts is None
    else:
        assert journal['status'] == 'held'

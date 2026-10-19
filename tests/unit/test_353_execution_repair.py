"""Executable repair retains rejected evidence, finite budgets and clean generations."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import (
    begin_attempt,
    mark_attempt_ready,
    prepare_repair,
    record_execution_failure,
    restore_attempt_generation,
    save_attempt,
)
from tests.unit.env_manifest_fixture import reviewed_cache, verification_output


def cache_fixture(tmp_path):
    return reviewed_cache(EnvCacheState(symphony_name='suite', sanitised_name='suite-abc', cache_dir=tmp_path/'suite-abc'))


def failed_report(cache, **change):
    data = json.loads(verification_output(cache.environment_manifest))
    data['results'][0].update(status='fail', exit_code=2, output='API_KEY=private-secret-value\nmissing installed artifact')
    data.update(change)
    return data


def started(cache, *, now=100, repairs=0):
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=now)
    budget.calls = 4
    budget.reads = 2
    budget.execution_repairs = repairs
    save_attempt(cache, budget, executing=True)
    return budget


@pytest.mark.parametrize('confirmed', [False, None])
def test_unconfirmed_cleanup_never_requests_a_repair(tmp_path, confirmed):
    cache = cache_fixture(tmp_path)
    started(cache)
    assert not record_execution_failure(cache, failed_report(cache), cleanup_confirmed=confirmed, now=101)
    assert cache.environment_attempt['execution_repairs'] == 0
    assert cache.environment_attempt['status'] == 'held'


def test_failed_execution_uses_same_budget_and_new_empty_cache_generation(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache, repairs=1)
    old_path = cache.cache_dir
    old_path.mkdir()
    (old_path/'installer-only').write_text('must not be reused')
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    assert cache.environment_attempt['execution_repairs'] == 2
    previous, feedback = prepare_repair(cache)
    assert previous.digest() == cache.environment_attempt['rejected'][0]['manifest_digest']
    assert cache.cache_dir != old_path
    assert not cache.cache_dir.exists()
    assert cache.environment_verification is None
    assert 'private-secret-value' not in json.dumps(feedback)
    assert feedback['results'][0]['status'] == 'fail'
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=120)
    assert (budget.calls, budget.reads, budget.repairs) == (4, 2, 2)
    assert cache.environment_attempt['expires_at'] == 700


def test_third_repair_holds_without_renewing_counters(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache, repairs=2)
    assert not record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    assert len(cache.environment_attempt['rejected']) == 1
    assert cache.environment_attempt['execution_repairs'] == 2
    with pytest.raises(EnvironmentDiscoveryError, match=r'held|exhausted'):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=102)


@pytest.mark.parametrize('change', [
    {'manifest_digest': 'b'*64}, {'scope': 'qa'}, {'platform': 'linux/aarch64'}, {'results': []},
])
def test_wrong_or_incomplete_execution_cannot_authorize_repair(tmp_path, change):
    cache = cache_fixture(tmp_path)
    started(cache)
    assert not record_execution_failure(cache, failed_report(cache, **change), cleanup_confirmed=True, now=101)
    assert not cache.cache_dir_ready


def test_wholly_unrun_report_does_not_request_repair(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    report = failed_report(cache)
    report['results'][0].update(status='not_run', exit_code=None)
    assert not record_execution_failure(cache, report, cleanup_confirmed=True, now=101)


def test_wall_clock_expiry_or_backwards_clock_never_renews_attempt(tmp_path):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    for now in (99, 701):
        with pytest.raises(EnvironmentDiscoveryError, match=r'deadline|clock'):
            begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=now)
    assert cache.environment_attempt['expires_at'] == 700


def test_repair_journal_and_generation_survive_real_snapshot_roundtrip(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    prepare_repair(cache)
    snapshots = _persist_env_cache({'suite': cache})
    restored = cache_fixture(tmp_path)
    _rehydrate_env_cache({'env_cache': {'suite': restored}}, SimpleNamespace(env_cache=snapshots))
    assert restored.environment_attempt == cache.environment_attempt
    assert restored.sanitised_name == cache.sanitised_name
    assert restored.cache_dir == cache.cache_dir
    assert begin_attempt(restored, DiscoveryLimits(), outer_seconds=3600, now=120).repairs == 1


def test_generation_cannot_restore_outside_configured_cache_parent(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    cache.environment_attempt['base_cache_dir'] = '/outside/other'
    with pytest.raises(EnvironmentDiscoveryError, match='generation'):
        restore_attempt_generation(cache)


def test_ready_journal_does_not_clear_rejected_history(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    prepare_repair(cache)
    reviewed_cache(cache)
    cache.cache_dir_ready = True
    cache.provenance_digest = "f"*64
    mark_attempt_ready(cache, now=102)
    assert len(cache.environment_attempt['rejected']) == 1
    assert cache.environment_attempt['status'] == 'ready'


def test_duplicate_failure_cannot_consume_a_second_repair_for_the_same_execution(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    report = failed_report(cache)
    assert record_execution_failure(cache, report, cleanup_confirmed=True, now=101)
    assert not record_execution_failure(cache, report, cleanup_confirmed=True, now=102)
    assert len(cache.environment_attempt['rejected']) == 1
    assert cache.environment_attempt['execution_repairs'] == 1


def test_saving_stale_counters_cannot_renew_the_budget(tmp_path):
    from coordinare.services.environment_attempt import AttemptBudget
    cache = cache_fixture(tmp_path)
    started(cache, repairs=1)
    save_attempt(cache, AttemptBudget())
    assert cache.environment_attempt['calls'] == 4
    assert cache.environment_attempt['reads'] == 2
    assert cache.environment_attempt['execution_repairs'] == 1


def test_ready_marker_cannot_waive_missing_execution_or_freeze(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    cache.environment_verification = None
    with pytest.raises(EnvironmentDiscoveryError, match='readiness'):
        mark_attempt_ready(cache)


def test_repair_cannot_rebind_failed_report_to_a_changed_target_image(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    cache.environment_capabilities['image_ref'] = 'trusted/changed@sha256:'+'b'*64
    assert not record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)


def test_expired_attempt_cannot_be_marked_ready(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    cache.cache_dir_ready = True
    cache.provenance_digest = 'f'*64
    with pytest.raises(EnvironmentDiscoveryError, match='deadline'):
        mark_attempt_ready(cache, now=701)


def test_restart_during_execution_holds_until_cleanup_is_confirmed(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    snapshots = _persist_env_cache({'suite': cache})
    restored = cache_fixture(tmp_path)
    _rehydrate_env_cache({'env_cache': {'suite': restored}}, SimpleNamespace(env_cache=snapshots))
    assert restored.environment_attempt['status'] == 'held'
    assert not restored.cache_dir_ready
    restored.bootstrap_commit_sha = 'b'*40
    with pytest.raises(EnvironmentDiscoveryError, match='cleanup'):
        begin_attempt(restored, DiscoveryLimits(), outer_seconds=3600, now=120)


def test_a_later_attempt_still_uses_a_never_reused_cache_generation(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    prepare_repair(cache)
    first = cache.cache_dir
    reviewed_cache(cache)
    cache.cache_dir_ready = True
    cache.provenance_digest = 'f'*64
    mark_attempt_ready(cache, now=102)
    started(cache, now=200)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=201)
    prepare_repair(cache)
    assert cache.cache_dir != first
    snapshots = _persist_env_cache({'suite': cache})
    restored = cache_fixture(tmp_path)
    _rehydrate_env_cache({'env_cache': {'suite': restored}}, SimpleNamespace(env_cache=snapshots))
    assert restored.cache_dir == cache.cache_dir


def test_a_successful_later_attempt_restores_the_actual_generation(tmp_path):
    cache = cache_fixture(tmp_path)
    started(cache)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    prepare_repair(cache)
    reviewed_cache(cache)
    cache.cache_dir_ready = True
    cache.provenance_digest = 'f'*64
    mark_attempt_ready(cache, now=102)
    started(cache, now=200)
    mark_attempt_ready(cache, now=201)
    restored = cache_fixture(tmp_path)
    _rehydrate_env_cache({'env_cache': {'suite': restored}},
        SimpleNamespace(env_cache=_persist_env_cache({'suite': cache})))
    assert restored.cache_dir == cache.cache_dir
    assert restored.sanitised_name == cache.sanitised_name


def test_category_counters_survive_restart(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    budget.reserve_repair('schema')
    budget.reserve_repair('coverage')
    budget.reserve_repair('coverage')
    save_attempt(cache, budget)
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=120)
    assert (resumed.schema_repairs, resumed.coverage_revisions, resumed.execution_repairs) == (1, 2, 0)
    assert cache.environment_attempt['version'] == 7
    assert cache.environment_attempt['expires_at'] == 700


def test_lowered_limit_does_not_replenish_consumption(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    budget.reserve_repair('coverage')
    save_attempt(cache, budget)
    with pytest.raises(EnvironmentDiscoveryError, match='coverage'):
        begin_attempt(cache, DiscoveryLimits(max_coverage_revisions=0), outer_seconds=3600, now=120)
    assert cache.environment_attempt['coverage_revisions'] == 1


@pytest.mark.parametrize('status', ['discovering', 'held', 'executing', 'repair_pending'])
def test_legacy_pending_requires_explicit_restart(tmp_path, status):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    current = cache.environment_attempt
    legacy = {k:v for k,v in current.items() if k not in {
        'schema_repairs', 'coverage_revisions', 'execution_repairs',
        'max_schema_repairs', 'max_coverage_revisions', 'max_execution_repairs'}}
    legacy.update(version=1, repairs=1, max_repairs=2, status=status)
    cache.environment_attempt = legacy
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=120)
    assert exc.value.code == 'legacy_attempt_requires_restart'
    assert cache.environment_attempt == legacy


def test_legacy_ready_generation_requires_findings_policy_refresh(tmp_path):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    legacy = {k:v for k,v in cache.environment_attempt.items() if k not in {
        'schema_repairs', 'coverage_revisions', 'execution_repairs',
        'max_schema_repairs', 'max_coverage_revisions', 'max_execution_repairs'}}
    legacy.update(version=1, repairs=1, max_repairs=2, status='ready')
    cache.environment_attempt = legacy
    with pytest.raises(EnvironmentDiscoveryError, match='legacy'):
        restore_attempt_generation(cache, interrupted=True)
    assert cache.environment_attempt == legacy


@pytest.mark.parametrize('version', [8, '2', None])
def test_unknown_journal_version_holds(tmp_path, version):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    cache.environment_attempt['version'] = version
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=120)


def test_durable_diagnostics_keep_actual_stage_and_category_counts(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    budget.stage = 'review'
    budget.reserve_repair('schema')
    budget.mandatory_inputs_required = 8
    budget.mandatory_inputs_delivered = 8
    save_attempt(cache, budget)
    summary = cache.environment_attempt['diagnostics']
    assert summary['stage'] == 'review'
    assert summary['schema_repairs'] == 1
    assert summary['mandatory_inputs_required'] == summary['mandatory_inputs_delivered'] == 8
    assert summary['max_execution_repairs'] == 2


def activation_failure_case(tmp_path):
    from coordinare_service_inference.environment.models import ExecutionCapabilities

    from tests.unit.env_manifest_fixture import reviewed_metadata
    cache = cache_fixture(tmp_path)
    metadata = reviewed_metadata(capabilities=ExecutionCapabilities.model_validate(cache.environment_capabilities),
        symphony_name=cache.symphony_name, activation={'path':['{cache}/missing-bin']})
    cache.environment_manifest = metadata['environment_manifest']
    cache.environment_assessment = metadata['environment_assessment']
    started(cache)
    report = failed_report(cache)
    report['results'][0].update(status='not_run', exit_code=None)
    activation = {'id':'activation','status':'fail','required':True,'exit_code':None,
        'manifest_digest':report['manifest_digest'],'context':'','output':'declared activation path missing'}
    report['results'].append(activation)
    return cache, report


def test_known_activation_failure_admits_repair_without_waiving_unrun_checks(tmp_path):
    cache, report = activation_failure_case(tmp_path)
    assert record_execution_failure(cache, report, cleanup_confirmed=True, now=101)
    assert cache.environment_attempt['status'] == 'repair_pending'
    assert cache.environment_attempt['execution_repairs'] == 1
    assert cache.environment_attempt['expires_at'] == 700
    rejected = cache.environment_attempt['rejected'][0]['report']
    assert not rejected['passed']
    assert rejected['results'][0]['status'] == 'not_run'
    assert rejected['results'][1]['status'] == 'fail'
    assert not cache.cache_dir_ready


@pytest.mark.parametrize('mutation',['unknown','optional','pass','indeterminate','exit','digest','scope','no_activation'])
def test_malformed_or_unknown_activation_evidence_still_holds(tmp_path, mutation):
    cache, report = activation_failure_case(tmp_path)
    report['results'][0].update(status='fail',exit_code=2)
    activation=report['results'][1]
    if mutation == 'no_activation':
        from coordinare_service_inference.environment.models import EnvironmentManifest
        cache.environment_manifest['activation']={'path':[],'env':{}}
        digest=EnvironmentManifest.model_validate(cache.environment_manifest).digest()
        cache.environment_assessment['manifest_digest']=digest
        report['manifest_digest']=digest
        for result in report['results']:
            result['manifest_digest']=digest
    else:
        key,value={'unknown':('id','unrecognized'), 'optional':('required',False), 'pass':('status','pass'),
            'indeterminate':('status','indeterminate'),'exit':('exit_code',2),'digest':('manifest_digest','f'*64),
            'scope':('context','qa')}[mutation]
        activation[key]=value
    assert not record_execution_failure(cache,report,cleanup_confirmed=True,now=101)
    assert cache.environment_attempt['status']=='held'
    assert cache.environment_attempt['execution_repairs']==0


def test_bounded_repair_excerpt_retains_terminal_error_after_verbose_progress(tmp_path):
    cache=cache_fixture(tmp_path)
    started(cache)
    report=failed_report(cache)
    report['results'][0]['output']='progress: dependency download\n'*100+'API_KEY=private-secret-value\nterminal action failure\n'
    assert record_execution_failure(cache,report,cleanup_confirmed=True,now=101)
    result=cache.environment_attempt['rejected'][0]['report']['results'][0]
    assert result['output'].endswith('terminal action failure\n')
    assert len(result['output'])<=512 and result['output_truncated']
    assert 'private-secret-value' not in result['output']
    assert result['status']=='fail' and not cache.cache_dir_ready

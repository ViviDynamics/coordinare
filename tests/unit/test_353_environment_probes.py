"""Probe observations are bounded and cannot manufacture runtime identity."""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_353_discovery_repair import CAPABILITIES, COMMIT, PATH


def request_data():
    return {'schema_version': 1, 'commands': [{'id': 'tool', 'argv': ['unfamiliar-tool', '--version'],
        'timeout_seconds': 30.0}], 'citations': {'tool': [PATH]}, 'questions': {'tool': 'Is the required tool available?'}}


def plan():
    from coordinare.services.environment_probes import ProbePlan, ProbeRequest
    return ProbePlan(execution_source_digest="1"*64, request=ProbeRequest.model_validate(request_data()), source_commit=COMMIT,
        inventory_digest='b'*64, inputs={PATH:'c'*64}, capabilities=CAPABILITIES)


def report_data(p=None):
    p = p or plan()
    return {'schema_version':1, 'execution_source_digest':p.execution_source_digest, 'plan':p.model_dump(mode='json'), 'plan_digest':p.digest(), 'source_commit':COMMIT,
        'image_ref':CAPABILITIES.image_ref, 'platform':CAPABILITIES.platform,
        'results':[{'id':'tool', 'status':'completed', 'exit_code':127, 'output':'not found', 'truncated':False}],
        'cleanup_confirmed':True}


@pytest.mark.parametrize('mutation', ['many', 'duplicate', 'scope', 'optional', 'dependency', 'timeout', 'identity', 'hook'])
def test_invalid_probe_request_is_rejected(mutation):
    from coordinare.services.environment_probes import ProbeRequest
    data = request_data()
    command = data['commands'][0]
    if mutation == 'many':
        data['commands'] = [dict(command, id=f'c{i}') for i in range(17)]
    elif mutation == 'duplicate':
        data['commands'].append(copy.deepcopy(command))
    elif mutation == 'identity':
        data['source_commit'] = COMMIT
    else:
        key, value = {'scope':('scope',['qa']), 'optional':('required',False),
            'dependency':('depends_on',['other']), 'timeout':('timeout_seconds',float('inf')),
            'hook':('env',{'BASH_ENV':'/bad'})}[mutation]
        command[key] = value
    with pytest.raises(ValidationError):
        ProbeRequest.model_validate(data)


def test_absent_executable_is_an_observation_not_a_pass():
    from coordinare.services.environment_probes import ProbeReport, validate_probe_report
    p = plan()
    report = ProbeReport.model_validate(report_data(p))
    validate_probe_report(p, report)
    assert report.results[0].exit_code == 127
    with pytest.raises(ValidationError):
        type(p)(request=p.request, source_commit=COMMIT, inventory_digest='b'*64, inputs={}, capabilities=CAPABILITIES)


@pytest.mark.parametrize('mutation', ['identity', 'missing', 'duplicate', 'exit', 'output', 'cleanup'])
def test_untrusted_probe_report_holds(mutation):
    from coordinare.services.environment_probes import ProbeReport, validate_probe_report
    p = plan()
    data = report_data(p)
    if mutation == 'identity':
        data['platform'] = 'linux/other'
    elif mutation == 'missing':
        data['results'] = []
    elif mutation == 'duplicate':
        data['results'] *= 2
    elif mutation == 'exit':
        data['results'][0]['exit_code'] = None
    elif mutation == 'output':
        data['results'][0]['output'] = '\u00e9'*8193
    elif mutation == 'cleanup':
        data['cleanup_confirmed'] = False
    with pytest.raises((ValidationError, EnvironmentDiscoveryError)):
        validate_probe_report(p, ProbeReport.model_validate(data))


def test_probe_budget_survives_save_and_resume(tmp_path):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.reserve_probe_batch()
    save_attempt(cache, budget)
    restored = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    assert restored.probe_batches == 1
    restored.reserve_probe_batch()
    with pytest.raises(EnvironmentDiscoveryError, match='probe'):
        restored.reserve_probe_batch()


def test_probe_request_reservation_is_bounded():
    budget = AttemptBudget()
    budget.reserve_probe_batch()
    budget.reserve_probe_batch()
    with pytest.raises(EnvironmentDiscoveryError):
        budget.reserve_probe_batch()


def test_probe_ownership_refuses_overlap_and_retains_report(tmp_path):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_probes import ProbeReport
    from coordinare.services.environment_repair import (
        begin_attempt,
        finish_probe,
        reserve_probe,
        save_attempt,
    )
    from tests.unit.test_353_execution_repair import cache_fixture
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.reserve_probe_batch()
    save_attempt(cache, budget)
    p = plan()
    reserve_probe(cache, p)
    with pytest.raises(EnvironmentDiscoveryError):
        reserve_probe(cache, p)
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    data = report_data(p)
    data.pop('plan', None)
    finish_probe(cache, ProbeReport.model_validate(data))
    assert cache.environment_attempt['probe_reports'][0]['plan'] == p.model_dump(mode='json')
    assert cache.environment_attempt['probe_owner'] is None
    assert len(cache.environment_attempt['probe_reports']) == 1
    assert begin_attempt(cache, DiscoveryLimits(), outer_seconds=600).probe_batches == 1


def test_pending_version_two_journal_requires_explicit_restart(tmp_path):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt
    from tests.unit.test_353_execution_repair import cache_fixture
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    cache.environment_attempt['version'] = 2
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    assert exc.value.code == 'legacy_attempt_requires_restart'


def test_probe_timeout_schema_matches_accepted_default():
    from coordinare.services.environment_probes import ProbeRequest
    data = request_data()
    del data['commands'][0]['timeout_seconds']
    request = ProbeRequest.model_validate(data)
    assert request.commands[0].timeout_seconds == 30
    schema = ProbeRequest.model_json_schema()
    command_name = schema['properties']['commands']['items']['$ref'].split('/')[-1]
    timeout = schema['$defs'][command_name]['properties']['timeout_seconds']
    assert timeout['default'] == timeout['maximum'] == 30


def test_operator_restart_cannot_discard_probe_ownership(tmp_path):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import (
        begin_attempt,
        reserve_probe,
        restart_environment_attempt,
        save_attempt,
    )
    from tests.unit.test_353_execution_repair import cache_fixture
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.reserve_probe_batch()
    save_attempt(cache, budget)
    reserve_probe(cache, plan())
    with pytest.raises(EnvironmentDiscoveryError, match='cleanup'):
        restart_environment_attempt(cache)
    assert cache.environment_attempt['probe_owner'] is not None


def test_dispatch_refuses_probe_owner_even_after_source_change(tmp_path):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import (
        begin_attempt,
        dispatch_refusal,
        reserve_probe,
        save_attempt,
    )
    from tests.unit.test_353_execution_repair import cache_fixture
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.reserve_probe_batch()
    save_attempt(cache, budget)
    reserve_probe(cache, plan())
    assert dispatch_refusal(cache, 'b'*40) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed', [True, False])
async def test_operator_request_reconciles_exact_probe_before_reset(tmp_path, monkeypatch, confirmed):
    from types import SimpleNamespace

    from coordinare.services.env_bootstrap_control import request_env_bootstrap_reset
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, reserve_probe, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.reserve_probe_batch()
    save_attempt(cache, budget)
    p = plan()
    reserve_probe(cache, p)
    reconciled = []
    async def cleanup(given):
        reconciled.append(given.digest())
        return confirmed
    monkeypatch.setattr('coordinare.services.env_bootstrap_control.confirm_probe_cleanup', cleanup)
    activity = SimpleNamespace(record=lambda **kwargs:None)
    if confirmed:
        await request_env_bootstrap_reset({}, 'suite', cache, 'test', activity)
        assert cache.environment_attempt is None
    else:
        with pytest.raises(EnvironmentDiscoveryError, match='cleanup'):
            await request_env_bootstrap_reset({}, 'suite', cache, 'test', activity)
        assert cache.environment_attempt['probe_owner'] == p.digest()
    assert reconciled == [p.digest()]


def test_probe_schema_expresses_existing_unscoped_required_independent_rules():
    from coordinare.services.environment_probes import ProbeEnvelope
    schema = ProbeEnvelope.model_json_schema()
    command = schema['$defs']['ProbeCommand']['properties']
    assert command['required']['const'] is True
    assert command['scope']['maxItems'] == command['depends_on']['maxItems'] == 0


@pytest.mark.parametrize('mutation,code', [('missing_question','probe_id_maps'), ('empty_question','probe_cited_question'),
    ('optional','probe_required_true')])
def test_probe_feedback_names_rejected_constraint_without_source_values(mutation,code):
    from coordinare.services.environment_discovery import _validation_feedback
    from coordinare.services.environment_probes import ProbeRequest
    data = request_data()
    if mutation == 'missing_question':
        data['questions'] = {}
    elif mutation == 'empty_question':
        data['questions']['tool'] = ' '
    else:
        data['commands'][0]['required'] = False
    with pytest.raises(ValidationError) as caught:
        ProbeRequest.model_validate(data)
    feedback = _validation_feedback(caught.value)
    assert any(error['code'] == code for error in feedback)
    assert 'unfamiliar-tool' not in str(feedback)


def test_saved_probe_without_executed_plan_holds_before_fresh_review():
    from coordinare.services.environment_probes import validate_saved_probe_reports
    data=report_data()
    data.pop('plan',None)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        validate_saved_probe_reports([data],COMMIT,CAPABILITIES)
    assert caught.value.code=='probe_plan_missing'


def test_report_cannot_substitute_a_different_executed_plan():
    from coordinare.services.environment_probes import ProbeReport, validate_probe_report
    expected=plan()
    data=report_data(expected)
    data['plan']=expected.model_dump(mode='json')
    data['plan']['request']['commands'][0]['argv']=['different-observation']
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        validate_probe_report(expected,ProbeReport.model_validate(data))
    assert caught.value.code=='probe_report_invalid'


@pytest.mark.parametrize('mutation', ['unchanged', 'cache_path', 'install_network', 'guest_identity', 'resources'])
def test_saved_probe_requires_complete_current_capability_identity(mutation):
    from coordinare_service_inference.environment.models import ExecutionCapabilities

    from coordinare.services.environment_probes import ProbePlan, validate_saved_probe_reports
    from tests.unit.test_353_runtime_identity import native_data, policy_data

    capabilities = ExecutionCapabilities.model_validate({
        **native_data(), 'schema_version': 2, 'runtime': policy_data(),
    })
    data = plan().model_dump(mode='json')
    data['capabilities'] = capabilities.model_dump(mode='json')
    data['request']['execution_context'] = 'private_service'
    executed = ProbePlan.model_validate(data)
    report = report_data(executed)
    report.update(image_ref=capabilities.image_ref, platform=capabilities.platform)
    current = capabilities.model_dump(mode='json')
    if mutation == 'cache_path':
        current['cache_mount_path'] = '/different-cache'
    elif mutation == 'install_network':
        current['install_network'] = 'disabled' if capabilities.install_network == 'enabled' else 'enabled'
    elif mutation == 'guest_identity':
        current['runtime']['identity']['guest_base_sha256'] = 'f' * 64
    elif mutation == 'resources':
        current['runtime']['resources']['memory_mib'] = 512
    current = ExecutionCapabilities.model_validate(current)
    if mutation == 'unchanged':
        validate_saved_probe_reports([report], COMMIT, current)
        return
    assert current.digest() != capabilities.digest()
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        validate_saved_probe_reports([report], COMMIT, current)
    assert caught.value.code == 'probe_report_invalid'


@pytest.mark.parametrize('allow', [False, True])
def test_repaired_cache_retires_old_observations_without_reusing_them(allow):
    from coordinare.services.environment_probes import select_current_probe_reports

    previous = plan()
    report = report_data(previous)
    current = previous.capabilities.model_copy(update={'cache_mount_path': '/devenv/new-generation'})
    history = [copy.deepcopy(report)]
    if allow:
        assert select_current_probe_reports(history, COMMIT, current, allow_cache_relocation=True) == []
        assert history == [report]
    else:
        with pytest.raises(EnvironmentDiscoveryError):
            select_current_probe_reports(history, COMMIT, current)


def test_repaired_cache_cannot_retire_probe_evidence_for_other_runtime_changes():
    from coordinare.services.environment_probes import select_current_probe_reports

    previous = plan()
    changed = previous.capabilities.model_copy(update={
        'cache_mount_path': '/devenv/new-generation',
        'install_network': 'disabled' if previous.capabilities.install_network == 'enabled' else 'enabled'})
    with pytest.raises(EnvironmentDiscoveryError):
        select_current_probe_reports([report_data(previous)], COMMIT, changed, allow_cache_relocation=True)


def test_an_execution_repair_relocates_the_cache_even_when_nothing_was_executed():
    """v36: an installer that failed its template preflight left no executed manifest, and the repair then held."""
    from types import SimpleNamespace

    from coordinare.services.environment_discovery import _execution_repair

    assert _execution_repair(SimpleNamespace(previous_manifest=object(), execution_feedback={'results': []}))
    assert not _execution_repair(SimpleNamespace(previous_manifest=None, execution_feedback=None))
    assert not _execution_repair(SimpleNamespace(previous_manifest=object(), execution_feedback=None))

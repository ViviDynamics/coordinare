"""Runtime observations inform manifests without granting readiness authority."""
from __future__ import annotations

import copy
import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import discover_and_review
from tests.unit.test_353_discovery_repair import (
    CAPABILITIES,
    COMMIT,
    PATH,
    SOURCE,
    assessment,
    candidate,
)
from tests.unit.test_353_environment_probes import report_data, request_data


@pytest.mark.asyncio
async def test_probe_observation_reaches_fresh_review_and_preserves_required_check():
    from coordinare.services.environment_probes import ProbeReport
    budget = AttemptBudget()
    async def fetch(path):
        return SOURCE
    async def execute(plan):
        assert budget.probe_batches == 1
        return ProbeReport.model_validate(report_data(plan))
    async def chat(messages):
        context = json.loads(messages[1]['content'])
        if 'manifest' in context:
            assert context['probe_evidence'][0]['results'][0]['exit_code'] == 127
            assert context['probe_evidence'][0]['plan_digest']
            assert len(messages) == 2
            return json.dumps(assessment(candidate()))
        if len(messages) == 2:
            return json.dumps({'tool':'read_file', 'path':PATH})
        if len(messages) == 4:
            return json.dumps({'probe':request_data()})
        assert json.loads(messages[-1]['content'])['probe_result']['results'][0]['exit_code'] == 127
        return json.dumps({'manifest':candidate()})
    manifest, review = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH],
        fetch=fetch, chat=chat, capabilities=CAPABILITIES, budget=budget, probe_executor=execute)
    assert review.manifest_digest == manifest.digest()
    assert manifest.checks[0].required
    assert set(manifest.inputs) == {PATH}


@pytest.mark.asyncio
async def test_no_probe_executor_means_hold_without_host_execution():
    async def fetch(path):
        return SOURCE
    async def chat(messages):
        return json.dumps({'probe':request_data()})
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH], fetch=fetch, chat=chat,
            capabilities=CAPABILITIES)
    assert exc.value.code == 'probe_executor_unavailable'


@pytest.mark.parametrize('change', ['removed', 'optional', 'scope'])
def test_revision_preserves_install_assertion_obligations(change):
    from coordinare_service_inference.environment.models import EnvironmentManifest

    from coordinare.services.environment_discovery import _preserve_obligations
    from tests.unit.test_351_environment_discovery import candidate as full_candidate
    data = full_candidate()
    data['install'] = [{'id':'assert-artifact', 'argv':['test','-x','{cache}/arbitrary/tool']}]
    previous = EnvironmentManifest.model_validate(data)
    revised = copy.deepcopy(data)
    if change == 'removed':
        revised['install'] = []
    elif change == 'optional':
        revised['install'][0]['required'] = False
    else:
        revised['install'][0]['scope'] = ['qa']
    with pytest.raises(EnvironmentDiscoveryError):
        _preserve_obligations(EnvironmentManifest.model_validate(revised), previous)


@pytest.mark.asyncio
async def test_missing_install_output_blocks_its_consumer(tmp_path):
    from coordinare_service_inference.environment.models import EnvironmentManifest
    from coordinare_service_inference.environment.runner import run_manifest

    from tests.unit.test_351_environment_discovery import candidate as full_candidate
    data = full_candidate()
    data['install'] = [{'id':'producer', 'argv':['true']},
        {'id':'assert-output', 'argv':['test','-d','{cache}/missing'], 'depends_on':['producer']},
        {'id':'consumer', 'argv':['touch','{cache}/consumed'], 'depends_on':['assert-output']}]
    result = await run_manifest(EnvironmentManifest.model_validate(data), repo=tmp_path, cache=tmp_path,
        scratch=tmp_path, install=True)
    assert not (tmp_path/'consumed').exists()
    assert result.passed is False


@pytest.mark.asyncio
async def test_owned_probe_checkpoints_before_executor_and_redacts_evidence(tmp_path, monkeypatch):
    import hashlib

    from coordinare.services import environment_probe_service as service
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_probes import ProbeReport
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_environment_probes import plan
    from tests.unit.test_353_execution_repair import cache_fixture
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.reserve_probe_batch()
    async def checkpoint():
        save_attempt(cache, budget)
    budget.checkpoint = checkpoint
    await budget.flush()
    p = plan().model_copy(update={'inputs':{PATH:hashlib.sha256(SOURCE.encode()).hexdigest()}})
    async def fetch(path):
        return SOURCE
    async def execute(plan, **kwargs):
        assert cache.environment_attempt['probe_owner'] == plan.digest()
        assert cache.environment_attempt['reads'] == 1
        data = report_data(plan)
        data['results'][0]['output'] = 'API_KEY=private-secret-value'
        return ProbeReport.model_validate(data)
    async def prepare_source(*args, **kwargs):
        from tests.unit.test_353_execution_source_pipeline import bundle_fixture
        assert cache.environment_attempt['probe_owner'] == p.digest()
        return bundle_fixture(tmp_path, {PATH:(SOURCE.encode(), '100644')})
    monkeypatch.setattr(service, 'prepare_repository_source', prepare_source)
    monkeypatch.setattr(service, 'execute_environment_probe', execute)
    result = await service.run_owned_probe(p, context=service.ProbeServiceContext(cache, {}, fetch, budget, DiscoveryLimits()))
    assert 'private-secret-value' not in result.model_dump_json()
    assert cache.environment_attempt['probe_owner'] is None
    assert len(cache.environment_attempt['probe_reports']) == 1


@pytest.mark.asyncio
async def test_probe_checkpoint_failure_prevents_runtime(tmp_path):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_probe_service import ProbeServiceContext, run_owned_probe
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_environment_probes import plan
    from tests.unit.test_353_execution_repair import cache_fixture
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.reserve_probe_batch()
    save_attempt(cache, budget)
    async def fail():
        raise EnvironmentDiscoveryError('checkpoint failed', code='attempt_checkpoint_failed')
    async def fetch(path):
        pytest.fail('failed checkpoint must prevent all external reads')
    budget.checkpoint = fail
    with pytest.raises(EnvironmentDiscoveryError, match='checkpoint'):
        await run_owned_probe(plan(), context=ProbeServiceContext(cache, {}, fetch, budget, DiscoveryLimits()))


@pytest.mark.asyncio
async def test_unread_probe_citation_feedback_identifies_rule_without_echoing_untrusted_value():
    from types import SimpleNamespace

    from coordinare.services.environment_discovery import _probe_reply
    from coordinare.services.environment_probes import ProbePlan, ProbeRequest
    from tests.unit.test_353_environment_probes import plan

    valid = plan()
    data = request_data()
    data['citations']['tool'] = ['untrusted-secret-source']
    executed = False
    async def probe(request):
        nonlocal executed
        ProbePlan(**{**valid.model_dump(), 'request': ProbeRequest.model_validate(request)})
        executed = True
        raise AssertionError('unread citation must never execute')
    messages = []
    budget = AttemptBudget(expected_envelope='probe')
    await _probe_reply(data, json.dumps({'probe':data}), messages,
        SimpleNamespace(probe=probe, inputs={PATH:'c'*64}), budget)
    feedback = json.loads(messages[-1]['content'])
    error = feedback['validation_errors'][0]
    assert error['code'] == 'probe_citations_uninspected'
    assert error['allowed_sources'] == [PATH]
    assert 'read_files' in error['guidance']
    assert 'untrusted-secret-source' not in messages[-1]['content']
    assert budget.schema_repairs == 1
    assert not executed

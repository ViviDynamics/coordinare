"""Repair trials on a failed generation's trial disk (repair-trial-amendment.md, maintainer ruling 2026-11-11).

Dettmore e2e-81 spent its four execution repairs on mistakes in service health and check commands, each costing a full
execution whose installs had already passed. A trial runs only replacement services and named checks on the failed
generation's sealed disk; it is an observation for the author, never evidence.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import (
    CheckResult,
    Command,
    EnvironmentManifest,
    VerificationReport,
)
from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt, TrialReceipt
from pydantic import ValidationError

from coordinare.services.environment_repair_trial import (
    MAX_TRIALS_PER_REPAIR,
    TRIAL_INSTRUCTION,
    TrialRequest,
    trial_handler,
    trial_summary,
    trials_left,
    with_trial_tool,
)
from tests.unit.test_353_runtime_supervisor import runtime_spec


def receipt_data(**changes):
    return {'schema_version': 1, 'trial': True, 'manifest_digest': '1' * 64, 'source_digest': '2' * 64,
            'runtime_digest': runtime_spec().digest(),
            'artifact': {'path': 'trial.qcow2', 'sha256': '3' * 64, 'bytes': 10}, **changes}


def runtime_manifest():
    from tests.unit.env_manifest_fixture import reviewed_metadata

    raw = reviewed_metadata()['environment_manifest']
    raw = {key: value for key, value in raw.items() if key not in {'scope_contract', 'scope_contract_digest', 'criterion_checks', 'runtime'}}
    manifest = EnvironmentManifest.model_validate({**raw, 'schema_version': 2, 'policy_version': '353-v2',
                                                   'runtime': runtime_spec().model_dump()})
    install = Command.model_validate({'id': 'install-tool', 'argv': ['bash', '-c', 'true']})
    return manifest.model_copy(update={'install': [install]})


def command(identifier, script='true'):
    return {'id': identifier, 'argv': ['bash', '-c', script]}


def report(*results):
    return VerificationReport(manifest_digest='4' * 64, results=[CheckResult(**item) for item in results])


def test_a_trial_receipt_is_never_a_consumer_receipt():
    trial = TrialReceipt.model_validate(receipt_data())
    with pytest.raises(ValidationError):
        RuntimeArtifactReceipt.model_validate(trial.model_dump(mode='json'))
    with pytest.raises(ValidationError, match='another artifact'):
        TrialReceipt.model_validate(receipt_data(artifact={'path': 'installed.qcow2', 'sha256': '3' * 64, 'bytes': 10}))
    with pytest.raises(ValueError, match='failed generation'):
        trial.require_binding(manifest_digest='9' * 64, source_digest='2' * 64, runtime_digest=runtime_spec().digest())


def test_a_report_without_a_trial_disk_serializes_as_before():
    plain = report({'id': 'c', 'status': 'pass', 'required': True})
    assert 'trial_artifacts' not in plain.model_dump()
    sealed = plain.model_copy(update={'trial_artifacts': TrialReceipt.model_validate(receipt_data())})
    data = json.loads(sealed.model_dump_json())
    data.pop('passed')
    assert VerificationReport.model_validate(data).trial_artifacts == sealed.trial_artifacts


def test_a_failed_installer_may_carry_a_trial_disk_but_never_a_runtime_receipt():
    from coordinare.services import environment_runtime_evidence as evidence

    manifest = runtime_manifest()
    failed = report({'id': 'c', 'status': 'fail', 'required': True}).model_copy(update={
        'runtime_digest': manifest.runtime.digest(), 'runtime_phase': 'installer', 'runtime_cleanup': True,
        'platform': manifest.runtime.policy.identity.carrier_platform,
        'trial_artifacts': TrialReceipt.model_validate(receipt_data())})
    original = evidence.require_runtime_target
    try:
        evidence.require_runtime_target = lambda *_: None
        evidence.validate_failed_installer_runtime(failed, manifest, SimpleNamespace())
    finally:
        evidence.require_runtime_target = original


def test_installs_passed_ignores_services_and_checks():
    from coordinare_service_inference.environment.runtime_execution import installs_passed

    manifest = runtime_manifest()
    install_ids = [item.id for item in manifest.install]
    assert install_ids, 'the fixture installs something'
    passing = [{'id': identifier, 'status': 'pass', 'required': True} for identifier in install_ids]
    assert installs_passed(manifest, report(*passing, {'id': 'svc.health', 'status': 'fail', 'required': True}))
    assert not installs_passed(manifest, report(*passing[1:]))
    assert not installs_passed(manifest, report(*passing, {'id': 'activation', 'status': 'fail', 'required': True}))


@pytest.mark.asyncio
async def test_a_trial_purpose_needs_its_own_receipt_and_never_a_consumer_one(tmp_path):
    from coordinare_service_inference.environment.runtime_supervisor import open_runtime

    common = {'repo': tmp_path, 'cache': tmp_path, 'scratch': tmp_path, 'install': False,
              'deadline': time.monotonic() + 30, 'manifest_digest': '1' * 64, 'source_digest': '2' * 64}
    with pytest.raises(ValueError, match='artifact policy'):
        await open_runtime(runtime_spec(), artifacts=None, purpose='trial', **common)
    consumer = RuntimeArtifactReceipt.model_validate({
        'schema_version': 1, 'manifest_digest': '1' * 64, 'source_digest': '2' * 64,
        'runtime_digest': runtime_spec().digest(), 'sealed': True,
        'artifacts': [{'path': 'installed.qcow2', 'sha256': '3' * 64, 'bytes': 10}]})
    with pytest.raises(ValueError, match='artifact policy'):
        await open_runtime(runtime_spec(), artifacts=consumer, purpose='trial',
                           trial=TrialReceipt.model_validate(receipt_data()), **common)
    with pytest.raises(ValueError, match='artifact policy'):
        await open_runtime(runtime_spec(), artifacts=None, purpose='consumer',
                           trial=TrialReceipt.model_validate(receipt_data()), **common)


@pytest.mark.asyncio
async def test_a_trial_disk_is_sealed_only_after_installs_passed_and_the_run_failed():
    from coordinare_service_inference.environment.runtime_supervisor import seal_trial

    lease = SimpleNamespace(install=True, installation_passed=False, installs_passed=False,
                            manifest_digest='1' * 64, source_digest='2' * 64)
    for change in ({}, {'installation_passed': True, 'installs_passed': True}, {'installs_passed': True, 'install': False}):
        with pytest.raises(ValueError, match='trial identity'):
            await seal_trial(SimpleNamespace(**{**vars(lease), **change}), manifest_digest='1' * 64,
                             source_digest='2' * 64, deadline=time.monotonic() + 5)


@pytest.mark.asyncio
async def test_a_trial_may_change_only_services_and_checks(tmp_path):
    from coordinare_service_inference.environment.runtime_execution import execute_trial

    base = runtime_manifest()
    changed = base.model_copy(update={'install': [*base.install, base.install[0].model_copy(update={'id': 'more'})]})
    with pytest.raises(ValueError, match='only services and checks'):
        await execute_trial(base, changed, trial=TrialReceipt.model_validate(receipt_data()), cache=tmp_path,
                            repo=tmp_path, scratch=tmp_path, secret_names=(), execution_source_digest='2' * 64,
                            runtime_deadline=time.monotonic() + 30, commands=None)


def test_trial_runtime_controls_carry_only_the_deadline(monkeypatch):
    from coordinare_service_inference.environment.runtime_transport import consume_runtime_controls

    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', str(time.time() + 60))
    deadline, receipt = consume_runtime_controls(required=True, install=False, trial=True)
    assert deadline is not None and receipt is None
    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', str(time.time() + 60))
    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_ARTIFACTS', '{}')
    with pytest.raises(ValueError, match=r'trial cannot use a consumer receipt|receipt invalid'):
        consume_runtime_controls(required=True, install=False, trial=True)


class Budget(SimpleNamespace):
    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_a_trial_answers_without_spending_corrections_and_is_bounded():
    budget = Budget(execution_repairs=1, trial_runs=0)
    seen = []

    async def executor(request):
        seen.append(request)
        return report({'id': 'db.health', 'status': 'fail', 'required': True, 'exit_code': 1,
                       'output': 'connection refused password=hunter2secret'},
                      {'id': 'c-ok', 'status': 'pass', 'required': True})

    run = trial_handler(executor, budget)
    refused = await run({'services': [], 'checks': []})
    assert refused['refused'] and budget.trial_runs == 0 and not seen
    answer = await run({'checks': [command('c-ok')]})
    assert budget.trial_runs == 1 and isinstance(seen[0], TrialRequest)
    assert answer['passed'] is False
    assert answer['results'][0]['id'] == 'db.health' and 'untrusted_output' in answer['results'][0]
    assert answer['results'][1] == {'id': 'c-ok', 'status': 'pass'}
    assert answer['trials_left'] == MAX_TRIALS_PER_REPAIR - 1
    for _ in range(MAX_TRIALS_PER_REPAIR - 1):
        await run({'checks': [command('c-ok')]})
    assert (await run({'checks': [command('c-ok')]}))['trials_left'] == 0
    assert len(seen) == MAX_TRIALS_PER_REPAIR


@pytest.mark.asyncio
async def test_a_trial_that_cannot_run_is_an_answer_not_a_hold():
    async def broken(_request):
        message = 'no trial disk'
        raise RuntimeError(message)

    answer = await trial_handler(broken, Budget(execution_repairs=1, trial_runs=0))({'checks': [command('c')]})
    assert 'could not run' in answer['unavailable']


def test_trial_summary_bounds_and_redacts_output():
    long = 'x' * 5000 + ' token=abcdefghijklmnopqrstuvwxyz0123456789'
    summary = trial_summary(report({'id': 'c', 'status': 'fail', 'required': True, 'exit_code': 2, 'output': long}))
    assert len(summary['results'][0]['untrusted_output'].encode()) <= 1500
    assert 'abcdefghijklmnopqrstuvwxyz0123456789' not in summary['results'][0]['untrusted_output']


def test_the_trial_envelope_is_offered_beside_the_declaration():
    schema = with_trial_tool({'type': 'object', 'anyOf': [{'const': 'declaration'}], '$defs': {'X': {}}})
    assert schema['anyOf'][0]['required'] == ['trial']
    assert {'X', 'Command', 'Service'} <= set(schema['$defs'])
    assert 'never evidence' in TRIAL_INSTRUCTION


def test_trials_are_offered_only_in_a_repair_with_an_executor():
    from coordinare.services.environment_discovery import _attach_tools

    async def executor(_request):
        return report()

    for executor_given, repair, offered in ((executor, True, True), (executor, False, False), (None, True, False)):
        discovery = SimpleNamespace(budget=Budget(execution_repairs=1, trial_runs=0), trial=None, probe=None)
        messages = [{'role': 'system', 'content': '{}'}, {'role': 'user', 'content': '{}'}]
        options = SimpleNamespace(trial_executor=executor_given, probe_executor=None, require_preflight=False)
        _attach_tools(discovery, options, 'a' * 40, messages, repair=repair)
        assert (discovery.trial is not None) is offered
        assert ('repair_trial' in json.loads(messages[1]['content'])) is offered
    assert trials_left(Budget(execution_repairs=2, trial_runs=1)) == 2 * MAX_TRIALS_PER_REPAIR - 1


def test_the_harness_trial_manifest_replaces_services_and_runs_only_named_checks():
    from scripts.validate_environment_repositories import trial_manifest

    base = runtime_manifest()
    request = TrialRequest.model_validate({'checks': [{**command('c-new'), 'depends_on': ['gone', base.install[0].id]}]})
    manifest = trial_manifest(base, request)
    assert [check.id for check in manifest.checks] == ['c-new']
    assert manifest.checks[0].depends_on == []
    assert manifest.install == base.install and manifest.activation == base.activation
    assert manifest.runtime == base.runtime


def test_a_trial_on_a_scoped_manifest_is_composed_without_its_whole_check_mapping():
    """Dettmore e2e-83: the first live trial ran three diagnostic checks; revalidating the composed manifest refused it
    because the scope's required criteria map to checks the trial does not run, and the trial was unavailable."""
    from coordinare_service_inference.environment.runtime_execution import trial_manifest

    from tests.unit.env_manifest_fixture import reviewed_metadata

    scoped = EnvironmentManifest.model_validate(reviewed_metadata()['environment_manifest'])
    assert scoped.criterion_checks, 'the fixture maps scope criteria to checks'
    composed = trial_manifest(scoped, [], [Command.model_validate(command('probe:gems-layout'))])
    assert [check.id for check in composed.checks] == ['probe:gems-layout']
    with pytest.raises(ValidationError, match='scope'):
        EnvironmentManifest.model_validate(composed.model_dump(mode='json'))

"""A guest whose teardown is not confirmed still holds, but its command evidence and failed conditions survive.

A diagnostic install ran every command for an hour, then teardown was not clean; the runner raised before
returning its report, so the only evidence left was one exception line.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment import runtime_execution
from coordinare_service_inference.environment.models import CheckResult, VerificationReport
from coordinare_service_inference.environment.runtime_execution import (
    PrivateRuntimeIncompleteError,
    execute_with_runtime,
)


def _command(identifier):
    return SimpleNamespace(id=identifier, required=True)


def _report() -> VerificationReport:
    return VerificationReport(manifest_digest='a' * 64, results=[
        CheckResult(id='install-tool', required=True, status='pass'),
        CheckResult(id='check-tool', required=True, status='fail'),
    ])


async def _run(tmp_path, monkeypatch, cleanup, *, commands=None, seal=None):
    lease = SimpleNamespace(activation_env={}, installation_passed=False)

    async def close(**_kwargs):
        return cleanup

    lease.close = close

    async def opened(*_args, **_kwargs):
        return lease

    async def passthrough(_lease, operation):
        return await operation

    async def no_scan(_lease):
        return None

    monkeypatch.setattr(runtime_execution, 'open_runtime', opened)
    monkeypatch.setattr(runtime_execution, 'run_guarded', passthrough)
    monkeypatch.setattr(runtime_execution, '_scan_bounds', no_scan)
    spec = SimpleNamespace(digest=lambda: 'b' * 64, policy=SimpleNamespace(resources=SimpleNamespace(max_seconds=60)))
    manifest = SimpleNamespace(runtime=spec, digest=lambda: 'a' * 64, install=[_command('install-tool'), _command('install-late')],
                               checks=[_command('check-tool')], services=[])

    async def finished(*_args, **_kwargs):
        return _report()

    if seal is not None:
        monkeypatch.setattr(runtime_execution, 'seal_runtime', seal)
    return await execute_with_runtime(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True,
        scope='', secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None,
        runtime_deadline=None, runtime_lease=None, commands=commands or finished)


@pytest.mark.asyncio
async def test_unclean_teardown_keeps_the_report_and_names_the_failed_conditions(tmp_path, monkeypatch):
    cleanup = SimpleNamespace(confirmed=True, clean_shutdown=False, bounds_respected=True)
    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await _run(tmp_path, monkeypatch, cleanup)
    report = held.value.report
    assert report is not None
    assert [result.id for result in report.results] == ['install-tool', 'check-tool']
    assert report.runtime_cleanup is False
    assert report.runtime_incomplete == ['clean_shutdown']
    assert report.runtime_artifacts is None
    assert report.runtime_phase == 'installer' and report.runtime_digest == 'b' * 64
    dumped = report.model_dump(mode='json')
    assert dumped['runtime_cleanup'] is False and dumped['runtime_incomplete'] == ['clean_shutdown']


@pytest.mark.asyncio
async def test_a_clean_teardown_report_does_not_carry_the_new_field(tmp_path, monkeypatch):
    cleanup = SimpleNamespace(confirmed=True, clean_shutdown=True, bounds_respected=True)
    report = await _run(tmp_path, monkeypatch, cleanup)
    assert report.runtime_cleanup is True
    assert 'runtime_incomplete' not in report.model_dump(mode='json')


def test_a_report_without_runtime_never_serializes_the_field():
    assert 'runtime_incomplete' not in _report().model_dump(mode='json')


CLEAN = SimpleNamespace(confirmed=True, clean_shutdown=True, bounds_respected=True)


@pytest.mark.asyncio
async def test_an_exhausted_deadline_keeps_finished_results_and_marks_the_rest_not_run(tmp_path, monkeypatch):
    import asyncio

    async def slow(*_args, progress, **_kwargs):
        progress['install-tool'] = CheckResult(id='install-tool', required=True, status='pass', duration_seconds=9.0)
        await asyncio.sleep(60)

    async def deadline(_lease, operation):
        task = asyncio.ensure_future(operation)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        raise TimeoutError('private runtime execution deadline exhausted')

    monkeypatch.setattr(runtime_execution, 'run_guarded', deadline, raising=True)
    monkeypatch.setattr(runtime_execution, 'open_runtime', _opened(CLEAN))
    monkeypatch.setattr(runtime_execution, '_scan_bounds', _no_scan)
    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await execute_with_runtime(_manifest(), cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True, scope='',
            secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None, runtime_deadline=None,
            runtime_lease=None, commands=slow)
    report = held.value.report
    assert report.runtime_incomplete == ['deadline_exhausted'] and report.runtime_cleanup is True
    statuses = {result.id: result.status for result in report.results}
    assert statuses == {'install-tool': 'pass', 'install-late': 'not_run', 'check-tool': 'not_run'}
    assert report.passed is False


@pytest.mark.asyncio
async def test_a_failed_seal_keeps_an_all_passing_report_but_holds(tmp_path, monkeypatch):
    async def passing(*_args, **_kwargs):
        return VerificationReport(manifest_digest='a' * 64, results=[
            CheckResult(id='install-tool', required=True, status='pass')])

    async def seal(*_args, **_kwargs):
        raise RuntimeError('guest shutdown did not complete')

    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await _run(tmp_path, monkeypatch, CLEAN, commands=passing, seal=seal)
    report = held.value.report
    assert report.runtime_incomplete == ['seal_failed'] and report.runtime_artifacts is None
    assert report.passed is True  # every command passed; the hold comes from runtime_incomplete


def _manifest():
    spec = SimpleNamespace(digest=lambda: 'b' * 64, policy=SimpleNamespace(resources=SimpleNamespace(max_seconds=60)))
    return SimpleNamespace(runtime=spec, digest=lambda: 'a' * 64, install=[_command('install-tool'), _command('install-late')],
                           checks=[_command('check-tool')], services=[])


def _opened(cleanup):
    async def close(**_kwargs):
        return cleanup

    async def opened(*_args, **_kwargs):
        return SimpleNamespace(activation_env={}, installation_passed=False, close=close)

    return opened


async def _no_scan(_lease):
    return None


@pytest.mark.parametrize('phase', ['installer', 'consumer'])
def test_acceptance_refuses_any_incomplete_report_even_with_cleanup_confirmed(phase):
    from coordinare.services.environment_runtime_evidence import (
        validate_failed_installer_runtime,
        validate_runtime_evidence,
    )

    report = _report()
    report.runtime_incomplete = ['seal_failed']
    report.runtime_cleanup = True
    with pytest.raises(ValueError, match='incomplete'):
        validate_runtime_evidence(report, SimpleNamespace(runtime=None), SimpleNamespace(), phase=phase)
    with pytest.raises(ValueError, match='incomplete'):
        validate_failed_installer_runtime(report, SimpleNamespace(runtime=None), SimpleNamespace())

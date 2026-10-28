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


@pytest.mark.asyncio
async def test_a_runtime_failure_keeps_finished_results_and_service_steps_are_not_run(tmp_path, monkeypatch):
    import asyncio

    async def failing(*_args, progress, **_kwargs):
        progress['install-tool'] = CheckResult(id='install-tool', required=True, status='pass')
        await asyncio.sleep(60)

    async def guest_exit(_lease, operation):
        task = asyncio.ensure_future(operation)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        raise RuntimeError('private runtime guest exited unexpectedly')

    manifest = _manifest()
    manifest.services = [SimpleNamespace(init=None, start=_command('db-start'), health=_command('db-health'),
                                         stop=_command('db-stop'))]
    monkeypatch.setattr(runtime_execution, 'run_guarded', guest_exit)
    monkeypatch.setattr(runtime_execution, 'open_runtime', _opened(CLEAN))
    monkeypatch.setattr(runtime_execution, '_scan_bounds', _no_scan)
    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await execute_with_runtime(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True, scope='',
            secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None, runtime_deadline=None,
            runtime_lease=None, commands=failing)
    report = held.value.report
    assert report.runtime_incomplete == ['runtime_failed']
    statuses = {result.id: result.status for result in report.results}
    assert statuses['install-tool'] == 'pass'
    assert {statuses[name] for name in ('db-start', 'db-health', 'db-stop', 'check-tool', 'install-late')} == {'not_run'}


@pytest.mark.asyncio
async def test_a_bounds_scan_failure_after_the_commands_keeps_the_complete_report(tmp_path, monkeypatch):
    from coordinare_service_inference.environment.runtime_supervisor import RuntimeScanStoppedError

    async def stopped(_lease):
        raise RuntimeScanStoppedError

    async def passthrough(_lease, operation):
        return await operation

    async def finished(*_args, **_kwargs):
        return _report()

    monkeypatch.setattr(runtime_execution, 'run_guarded', passthrough)
    monkeypatch.setattr(runtime_execution, 'open_runtime', _opened(CLEAN))
    monkeypatch.setattr(runtime_execution, '_scan_bounds', stopped)
    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await execute_with_runtime(_manifest(), cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True,
            scope='', secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None,
            runtime_deadline=None, runtime_lease=None, commands=finished)
    assert held.value.report.runtime_incomplete == ['runtime_failed']
    assert [result.id for result in held.value.report.results] == ['install-tool', 'check-tool']


def test_an_incomplete_consumer_report_is_summarised_by_ids_and_codes_only():
    import json

    from coordinare.services.env_cache import _incomplete_summary

    out = json.dumps({'runtime_incomplete': ['deadline_exhausted'], 'results': [
        {'id': 'check-a', 'status': 'pass', 'output': 'secret-looking text'},
        {'id': 'check-b', 'status': 'fail', 'output': 'more text'},
        {'id': 'check-c', 'status': 'not_run'}]})
    summary = _incomplete_summary(out)
    assert summary == 'runtime incomplete: deadline_exhausted; results pass=1, fail=1, not_run=1; failed: check-b'
    assert 'text' not in summary
    assert _incomplete_summary('{"results": []}') is None
    assert _incomplete_summary('not json') is None


@pytest.mark.asyncio
async def test_a_pending_cancellation_is_never_turned_into_a_hold(tmp_path, monkeypatch):
    import asyncio

    async def cancelled_then_scan_fails(_lease, operation):
        operation.close()
        asyncio.current_task().cancel()
        raise RuntimeError('bounds scan failed while cancelling')

    monkeypatch.setattr(runtime_execution, 'run_guarded', cancelled_then_scan_fails)
    monkeypatch.setattr(runtime_execution, 'open_runtime', _opened(CLEAN))
    monkeypatch.setattr(runtime_execution, '_scan_bounds', _no_scan)

    async def unused(*_args, **_kwargs):
        return _report()

    run = asyncio.create_task(execute_with_runtime(_manifest(), cache=tmp_path, repo=tmp_path, scratch=tmp_path,
        install=True, scope='', secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None,
        runtime_deadline=None, runtime_lease=None, commands=unused))
    with pytest.raises(asyncio.CancelledError):
        await run


@pytest.mark.asyncio
async def test_a_failed_close_is_an_unconfirmed_teardown_that_keeps_the_evidence(tmp_path, monkeypatch):
    async def close(**_kwargs):
        raise TimeoutError('qemu did not exit after kill')

    async def opened(*_args, **_kwargs):
        return SimpleNamespace(activation_env={}, installation_passed=False, close=close)

    async def passthrough(_lease, operation):
        return await operation

    async def finished(*_args, **_kwargs):
        return _report()

    monkeypatch.setattr(runtime_execution, 'run_guarded', passthrough)
    monkeypatch.setattr(runtime_execution, 'open_runtime', opened)
    monkeypatch.setattr(runtime_execution, '_scan_bounds', _no_scan)
    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await execute_with_runtime(_manifest(), cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True,
            scope='', secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None,
            runtime_deadline=None, runtime_lease=None, commands=finished)
    report = held.value.report
    assert report.runtime_incomplete == ['unconfirmed'] and report.runtime_cleanup is False
    assert report.runtime_incomplete_detail == 'TimeoutError: qemu did not exit after kill'
    assert [result.id for result in report.results] == ['install-tool', 'check-tool']


@pytest.mark.asyncio
async def test_the_cause_of_a_runtime_failure_is_kept_bounded(tmp_path, monkeypatch):
    async def failing(_lease, operation):
        operation.close()
        raise ValueError('x' * 2000)

    monkeypatch.setattr(runtime_execution, 'run_guarded', failing)
    monkeypatch.setattr(runtime_execution, 'open_runtime', _opened(CLEAN))
    monkeypatch.setattr(runtime_execution, '_scan_bounds', _no_scan)

    async def unused(*_args, **_kwargs):
        return _report()

    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await execute_with_runtime(_manifest(), cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True,
            scope='', secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None,
            runtime_deadline=None, runtime_lease=None, commands=unused)
    detail = held.value.report.runtime_incomplete_detail
    assert detail == 'ValueError: ' + 'x' * 2000  # whole, bounded far above; the owner redacts and clips


@pytest.mark.asyncio
async def test_a_close_failure_during_cancellation_still_cancels(tmp_path, monkeypatch):
    import asyncio

    async def close(**_kwargs):
        asyncio.current_task().cancel()
        raise TimeoutError('qemu did not exit after kill')

    async def opened(*_args, **_kwargs):
        return SimpleNamespace(activation_env={}, installation_passed=False, close=close)

    async def passthrough(_lease, operation):
        return await operation

    async def finished(*_args, **_kwargs):
        return _report()

    monkeypatch.setattr(runtime_execution, 'run_guarded', passthrough)
    monkeypatch.setattr(runtime_execution, 'open_runtime', opened)
    monkeypatch.setattr(runtime_execution, '_scan_bounds', _no_scan)
    run = asyncio.create_task(execute_with_runtime(_manifest(), cache=tmp_path, repo=tmp_path, scratch=tmp_path,
        install=True, scope='', secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None,
        runtime_deadline=None, runtime_lease=None, commands=finished))
    with pytest.raises(asyncio.CancelledError):
        await run


@pytest.mark.asyncio
async def test_a_known_secret_value_in_the_cause_is_redacted_at_the_source(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMPLE_API_TOKEN', 'sample-secret-value-123456')

    async def failing(_lease, operation):
        operation.close()
        raise RuntimeError('guest console: token=sample-secret-value-123456')

    monkeypatch.setattr(runtime_execution, 'run_guarded', failing)
    monkeypatch.setattr(runtime_execution, 'open_runtime', _opened(CLEAN))
    monkeypatch.setattr(runtime_execution, '_scan_bounds', _no_scan)

    async def unused(*_args, **_kwargs):
        return _report()

    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await execute_with_runtime(_manifest(), cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True,
            scope='', secret_names=('SAMPLE_API_TOKEN',), execution_source_digest='c' * 64, runtime_artifacts=None,
            runtime_deadline=None, runtime_lease=None, commands=unused)
    assert 'sample-secret-value-123456' not in held.value.report.runtime_incomplete_detail


def test_repair_refuses_incomplete_evidence_before_filtering_it(tmp_path):
    from coordinare.services import environment_repair

    with pytest.raises(environment_repair.EnvironmentDiscoveryError, match='incomplete'):
        environment_repair._rejected_evidence(SimpleNamespace(), {'runtime_incomplete': ['runtime_failed'],
                                                                   'runtime_cleanup': True, 'results': []})


@pytest.mark.asyncio
async def test_a_crashed_consumer_detail_is_redacted():
    from coordinare.services import env_cache

    token = 'ghp_' + 'c' * 36

    class Crashed:
        returncode = 1

        async def communicate(self, input):
            return f'Traceback ...\nRuntimeError: guest exited; console: token={token}\n'.encode(), b''

    passed, detail = await env_cache._verify_outcome(Crashed(), 'verify-x', 'sym', 'script')
    assert passed is False and token not in detail


@pytest.mark.asyncio
async def test_a_token_split_by_the_clip_boundary_is_still_redacted():
    from coordinare.services import env_cache

    token = 'ghp_' + 'd' * 36

    class Crashed:
        returncode = 1

        async def communicate(self, input):
            # Place the token so a 300-character tail would cut through its prefix.
            return ('x' * 400 + 'token=' + token + ' ' + 'y' * 280).encode(), b''

    _, detail = await env_cache._verify_outcome(Crashed(), 'verify-x', 'sym', 'script')
    assert 'd' * 10 not in detail  # the clipped tail keeps 19 characters of the token unless redacted first


def test_result_json_evidence_has_redacted_outputs_and_the_original_is_untouched():
    from scripts import validate_environment_repositories as harness

    token = 'ghp_' + 'e' * 36
    evidence = {'results': [{'id': 'check-a', 'status': 'pass', 'output': 'token=' + token}]}
    copy = harness._redacted_evidence(evidence)
    assert token not in copy['results'][0]['output']
    assert evidence['results'][0]['output'] == 'token=' + token


def test_the_cause_is_never_clipped_before_pattern_redaction():
    from coordinare_service_inference.environment.runtime_execution import DETAIL_MAX_CHARS, _cause_detail

    from scripts import validate_environment_repositories as harness

    token = 'ghp_' + 'f' * 36
    quoted = 'password="alpha123 beta456 ' + 'z' * 40 + '"'
    message = 'a ' * 270 + token + ' ' + quoted
    detail = _cause_detail(RuntimeError(message), ())
    assert detail == 'RuntimeError: ' + message  # sent whole; only the owner can pattern-redact
    shown = harness._redacted_detail(detail)
    assert len(shown) <= 600 and 'f' * 5 not in shown and 'beta456' not in shown
    assert _cause_detail(RuntimeError('x' * (DETAIL_MAX_CHARS + 1)), ()) == 'RuntimeError'

"""Docker state left after the last command fails the records that should have removed it, and the run stays repairable.

Dettmore e2e-04: postgres-stop ran `docker rm -f dettmore-pg`, leaving the image's anonymous volume. The guest's seal
check refused to shut down with it present, so the whole run held as an incomplete runtime with no recorded cause,
and the execution repairs that would have fixed apt, the stop command and the rest never ran.
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

CLEAN = SimpleNamespace(confirmed=True, clean_shutdown=True, bounds_respected=True)
LISTING = b'containers=\nvolumes=0f3c9a\nnetworks=\n'


def _command(identifier, *argv):
    return SimpleNamespace(id=identifier, required=True, argv=list(argv or ['true']))


def _manifest(*, services=True):
    spec = SimpleNamespace(digest=lambda: 'b' * 64, policy=SimpleNamespace(resources=SimpleNamespace(max_seconds=60)))
    stop = _command('db-stop', 'docker', 'rm', '-f', 'db')
    return SimpleNamespace(runtime=spec, digest=lambda: 'a' * 64, install=[_command('install-tool')],
                           checks=[_command('check-tool'), _command('check-image', 'docker', 'run', 'img')],
                           services=[SimpleNamespace(id='db', stop=stop)] if services else [])


def _report(*, stop='pass'):
    results = [CheckResult(id='install-tool', required=True, status='pass'),
               CheckResult(id='check-tool', required=True, status='pass'),
               CheckResult(id='check-image', required=True, status='pass')]
    if stop is not None:
        results.append(CheckResult(id='db-stop', required=True, status=stop, output='removed'))
    return VerificationReport(manifest_digest='a' * 64, results=results)


async def _run(tmp_path, monkeypatch, *, report, manifest, listing=LISTING, cleanup=CLEAN, failing_list=False,
               shutdown_error=None, secret_names=(), redact_values=frozenset()):
    scripts: list[str] = []
    sealed: list[bool] = []
    lease = SimpleNamespace(activation_env={}, installation_passed=False, shutdown_error=None)

    async def ssh(script, *, deadline, check=True):
        scripts.append(script)
        if 'docker ps -a --format' in script:
            if failing_list:
                raise RuntimeError('guest command failed')
            return 0, listing
        return 0, b''

    async def close(**_kwargs):
        lease.shutdown_error = shutdown_error
        return cleanup

    async def opened(*_args, **_kwargs):
        return lease

    async def passthrough(_lease, operation):
        return await operation

    async def no_scan(_lease):
        return None

    async def seal(_lease, **_kwargs):
        sealed.append(True)

    async def finished(*_args, **_kwargs):
        return report

    lease.ssh, lease.close = ssh, close
    monkeypatch.setattr(runtime_execution, 'open_runtime', opened)
    monkeypatch.setattr(runtime_execution, 'run_guarded', passthrough)
    monkeypatch.setattr(runtime_execution, '_scan_bounds', no_scan)
    monkeypatch.setattr(runtime_execution, 'seal_runtime', seal)
    result = await execute_with_runtime(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True,
        scope='', secret_names=secret_names, execution_source_digest='c' * 64, runtime_artifacts=None,
        runtime_deadline=None, runtime_lease=None, commands=finished, redact_values=redact_values)
    return result, scripts, sealed


@pytest.mark.asyncio
async def test_a_volume_left_after_service_stop_fails_the_stop_and_is_removed_so_the_run_stays_repairable(
        tmp_path, monkeypatch):
    report, scripts, sealed = await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest())
    stop = next(result for result in report.results if result.id == 'db-stop')
    assert stop.status == 'fail'
    assert stop.output.startswith('removed\n') and '0f3c9a' in stop.output and 'docker rm -f -v' in stop.output
    assert {result.id for result in report.results if result.status == 'fail'} == {'db-stop'}
    assert not report.passed and sealed == []
    assert report.runtime_cleanup is True and report.runtime_incomplete is None
    assert any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_without_a_service_the_docker_commands_that_ran_are_blamed(tmp_path, monkeypatch):
    report, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=_report(stop=None), manifest=_manifest(services=False))
    assert {result.id for result in report.results if result.status == 'fail'} == {'check-image'}


@pytest.mark.asyncio
async def test_a_clean_guest_changes_nothing_and_still_seals(tmp_path, monkeypatch):
    clean = b'containers=\nvolumes=\nnetworks=\n'
    report, scripts, sealed = await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest(), listing=clean)
    assert report.passed and sealed == [True]
    assert not any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_state_no_record_could_have_left_is_not_removed(tmp_path, monkeypatch):
    """Removing it would let unexplained state pass silently; the seal check still refuses it."""
    manifest = _manifest(services=False)
    manifest.checks = [_command('check-tool')]
    report = VerificationReport(manifest_digest='a' * 64, results=[CheckResult(id='check-tool', required=True, status='pass')])
    _report_out, scripts, _sealed = await _run(tmp_path, monkeypatch, report=report, manifest=manifest)
    assert not any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_a_failed_listing_leaves_the_run_as_it_was(tmp_path, monkeypatch):
    report, scripts, sealed = await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest(), failing_list=True)
    assert report.passed and sealed == [True]
    assert not any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_a_refused_shutdown_records_its_cause(tmp_path, monkeypatch):
    unclean = SimpleNamespace(confirmed=True, clean_shutdown=False, bounds_respected=True)
    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest(), cleanup=unclean,
                   listing=b'containers=\nvolumes=\nnetworks=\n', shutdown_error='RuntimeError: guest command failed')
    assert held.value.report.runtime_incomplete == ['clean_shutdown']
    assert 'guest command failed' in held.value.report.runtime_incomplete_detail


@pytest.mark.asyncio
async def test_secret_values_in_leftover_names_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv('OPERATOR_TOKEN', 'operator-value-123')
    listing = b'containers=db-operator-value-123\nvolumes=gen-secret-456\nnetworks=\n'
    report, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest(), listing=listing,
                                           secret_names=('OPERATOR_TOKEN',), redact_values=frozenset({'gen-secret-456'}))
    stop = next(result for result in report.results if result.id == 'db-stop')
    assert stop.status == 'fail'
    assert 'operator-value-123' not in stop.output and 'gen-secret-456' not in stop.output
    assert '[REDACTED]' in stop.output


@pytest.mark.asyncio
async def test_the_live_lease_path_also_fails_the_stop_that_left_state(monkeypatch):
    lease = SimpleNamespace(spec=None, manifest_digest='a' * 64, source_digest='c' * 64, scratch='/tmp', deadline=1e12,
                            activation_env={}, install=True, installation_passed=False)
    removed: list[str] = []

    async def ssh(script, *, deadline, check=True):
        if 'docker ps -a --format' in script:
            return 0, LISTING
        removed.append(script)
        return 0, b''

    lease.ssh = ssh
    manifest = _manifest()
    lease.spec = manifest.runtime

    async def passthrough(_lease, operation):
        return await operation

    async def no_scan(_lease):
        return None

    async def finished(*_args, **_kwargs):
        return _report()

    monkeypatch.setattr(runtime_execution, 'run_guarded', passthrough)
    monkeypatch.setattr(runtime_execution, '_scan_bounds', no_scan)
    report = await execute_with_runtime(manifest, cache='/tmp', repo='/tmp', scratch='/tmp', install=True, scope='',
        secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None, runtime_deadline=None,
        runtime_lease=lease, commands=finished)
    assert not report.passed and lease.installation_passed is False
    assert any('docker volume rm' in script for script in removed)

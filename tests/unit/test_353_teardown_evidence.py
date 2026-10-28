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
    PrivateRuntimeTeardownError,
    execute_with_runtime,
)


def _report() -> VerificationReport:
    return VerificationReport(manifest_digest='a' * 64, results=[
        CheckResult(id='install-tool', required=True, status='pass'),
        CheckResult(id='check-tool', required=True, status='fail'),
    ])


async def _run(tmp_path, monkeypatch, cleanup):
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
    manifest = SimpleNamespace(runtime=spec, digest=lambda: 'a' * 64)

    async def commands(*_args, **_kwargs):
        return _report()

    return await execute_with_runtime(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True,
        scope='', secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None,
        runtime_deadline=None, runtime_lease=None, commands=commands)


@pytest.mark.asyncio
async def test_unclean_teardown_keeps_the_report_and_names_the_failed_conditions(tmp_path, monkeypatch):
    cleanup = SimpleNamespace(confirmed=True, clean_shutdown=False, bounds_respected=True)
    with pytest.raises(PrivateRuntimeTeardownError) as held:
        await _run(tmp_path, monkeypatch, cleanup)
    report = held.value.report
    assert report is not None
    assert [result.id for result in report.results] == ['install-tool', 'check-tool']
    assert report.runtime_cleanup is False
    assert report.runtime_teardown_failures == ['clean_shutdown']
    assert report.runtime_phase == 'installer' and report.runtime_digest == 'b' * 64
    dumped = report.model_dump(mode='json')
    assert dumped['runtime_cleanup'] is False and dumped['runtime_teardown_failures'] == ['clean_shutdown']


@pytest.mark.asyncio
async def test_a_clean_teardown_report_does_not_carry_the_new_field(tmp_path, monkeypatch):
    cleanup = SimpleNamespace(confirmed=True, clean_shutdown=True, bounds_respected=True)
    report = await _run(tmp_path, monkeypatch, cleanup)
    assert report.runtime_cleanup is True
    assert 'runtime_teardown_failures' not in report.model_dump(mode='json')


def test_a_report_without_runtime_never_serializes_the_field():
    assert 'runtime_teardown_failures' not in _report().model_dump(mode='json')

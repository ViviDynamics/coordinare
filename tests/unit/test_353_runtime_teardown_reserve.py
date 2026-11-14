"""Commands stop CLEANUP_SECONDS before a private runtime's deadline, so the guest can still shut down cleanly.

A check ran to its own 3600 s timeout and the later checks ran on to the run deadline. The run was held as
deadline_exhausted, and the guest was SIGKILLed because close() found no time left to shut it down.
"""
from __future__ import annotations

import asyncio
import gc
import sys
import time
import warnings
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment import runner, runtime_execution
from coordinare_service_inference.environment.models import EnvironmentManifest
from coordinare_service_inference.environment.runtime_execution import execute_with_runtime

from tests.unit.test_353_runtime_hash_lifecycle import lease_fixture

SLEEP_FOREVER = [sys.executable, '-c', 'import time; time.sleep(30)']


def contract(checks, **changes):
    data = {
        'schema_version': 1,
        'symphony_name': 'fixture',
        'source_commit': 'a' * 40,
        'inputs': {},
        'requirements': [],
        'install': [],
        'checks': checks,
        'activation': {},
        'services': [],
        'empty_reason': 'uses base image',
        'policy_version': '351-v1',
    }
    data.update(changes)
    return EnvironmentManifest.model_validate(data)


async def _no_scan(_lease):
    return None


@pytest.mark.asyncio
async def test_check_crossing_command_deadline_is_cut_short_and_later_checks_do_not_run(tmp_path):
    marker = tmp_path / 'stop'
    service = {
        'id': 'svc',
        'init': {'id': 'svc-init', 'argv': ['true']},
        'start': {'id': 'svc-start', 'argv': SLEEP_FOREVER},
        'health': {'id': 'svc-health', 'argv': ['true']},
        # A stop still runs after the checks are cut short: it removes state the guest must not keep.
        'stop': {'id': 'svc-stop', 'argv': [sys.executable, '-c',
                                            f'from pathlib import Path; Path({str(marker)!r}).touch()']},
        'required': False,
    }
    manifest = contract([
        {'id': 'slow-check', 'argv': SLEEP_FOREVER, 'timeout_seconds': 3600},
        {'id': 'late-check', 'argv': ['true']},
    ], services=[service])
    started = time.monotonic()
    report = await runner._run_manifest_commands(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path,
                                                 command_deadline=time.monotonic() + 1.0)
    assert time.monotonic() - started < 30
    by_id = {result.id: result for result in report.results}
    assert by_id['slow-check'].status == 'indeterminate'
    assert 'session deadline reached' in by_id['slow-check'].output
    assert by_id['late-check'].status == 'not_run'
    assert by_id['late-check'].output == runner.SESSION_DEADLINE_NOT_RUN
    assert marker.exists()


@pytest.mark.asyncio
async def test_overrunning_check_returns_ordinary_report_not_held_runtime(tmp_path, monkeypatch):
    # The reserve is shortened so the test runs in seconds; the rule is unchanged.
    monkeypatch.setattr(runtime_execution, 'CLEANUP_SECONDS', 1.5)
    monkeypatch.setattr(runtime_execution, '_scan_bounds', _no_scan)
    real = contract([
        {'id': 'slow-check', 'argv': SLEEP_FOREVER, 'timeout_seconds': 3600},
        {'id': 'late-check', 'argv': ['true']},
    ])
    spec = SimpleNamespace(digest=lambda: 'b' * 64, policy=SimpleNamespace(resources=SimpleNamespace(max_seconds=60)))
    manifest = SimpleNamespace(runtime=spec, digest=real.digest, install=real.install, checks=real.checks,
                               services=real.services)
    deadline = time.monotonic() + 3.0
    lease = SimpleNamespace(activation_env={}, deadline=deadline, shutdown_error=None, installation_passed=False)

    async def ssh(script, *, deadline, check=True):
        return 0, b'containers=\nvolumes=\nnetworks=\n' if script == runtime_execution.RESIDUAL_LIST else b''

    async def close(**_kwargs):
        return SimpleNamespace(confirmed=True, clean_shutdown=True, bounds_respected=True)

    async def opened(*_args, **_kwargs):
        return lease

    lease.ssh, lease.close = ssh, close
    monkeypatch.setattr(runtime_execution, 'open_runtime', opened)

    async def commands(_manifest, **kwargs):
        return await runner._run_manifest_commands(real, **kwargs)

    report = await execute_with_runtime(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=False,
        scope='', secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None,
        runtime_deadline=deadline, runtime_lease=None, commands=commands)
    assert not report.runtime_incomplete
    assert report.runtime_cleanup is True
    by_id = {result.id: result for result in report.results}
    assert by_id['slow-check'].status == 'indeterminate'
    assert by_id['late-check'].status == 'not_run'
    assert not report.passed


@pytest.mark.asyncio
async def test_expired_close_kills_guest_without_creating_stop_coroutine(tmp_path, monkeypatch):
    lease = lease_fixture(tmp_path)
    monkeypatch.setattr(lease, 'check_bounds', lambda **kwargs: None)
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)',
                                                   start_new_session=True)
    lease.process = process
    calls = []

    async def stop_guest(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(lease, 'stop_guest', stop_guest)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        report = await lease.close(deadline=time.monotonic() - 1)
        gc.collect()
    assert not calls
    assert lease.shutdown_error and lease.shutdown_error.startswith('TimeoutError')
    assert process.returncode is not None, 'guest was not killed'
    assert not [warning for warning in caught if 'never awaited' in str(warning.message)]
    assert not report.bounds_respected

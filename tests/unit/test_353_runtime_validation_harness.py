"""Field validation must transfer real sealed output before the clean consumer."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from scripts import validate_environment_repositories as harness

from coordinare.services.environment_acceptance import accepted_environment
from tests.unit.runtime_manifest_fixture import runtime_cache, runtime_report


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_install', [False, True])
async def test_field_validation_requires_installer_actions_and_transfers_sealed_receipt(tmp_path, monkeypatch, missing_install):
    cache = runtime_cache(tmp_path)
    manifest = accepted_environment(cache)
    report = runtime_report(cache, phase='installer')
    if missing_install:
        report['results'] = [item for item in report['results'] if item['id'] != 'install']
    snapshot = SimpleNamespace(name='sym', commit=manifest.source_commit, verify=AsyncMock())
    image = cache.environment_capabilities['image_ref']
    (tmp_path/'execution-approved.json').write_text(json.dumps({'scope': 'environment_validation',
        'approved': True, 'manifest_digest': manifest.digest(), 'source_commit': snapshot.commit, 'image_ref': image}))
    monkeypatch.setattr(harness, '_install', AsyncMock(return_value=(0, json.dumps(report).encode())))
    async def clean(*args, **kwargs):
        assert cache.environment_runtime_artifacts == report['runtime_artifacts']
        assert cache.environment_verification is None
        return None, 'synthetic test stops before real clean execution'
    verifier = AsyncMock(side_effect=clean)
    monkeypatch.setattr(harness, 'verify_env_cache_clean', verifier)
    service = Mock()
    result = {}
    if missing_install:
        with pytest.raises(ValueError):
            await harness.execute_accepted(snapshot, service, {'env_cache': {'sym': cache}}, image=image,
                target=tmp_path, profile=harness.Profile(model='fixture', base_url='http://localhost:1'), result=result)
        verifier.assert_not_awaited()
        assert cache.environment_runtime_artifacts is None
    else:
        await harness.execute_accepted(snapshot, service, {'env_cache': {'sym': cache}}, image=image,
            target=tmp_path, profile=harness.Profile(model='fixture', base_url='http://localhost:1'), result=result)
        assert result['installer_passed'] is True
        assert result['ready_at_validation'] is False
        verifier.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('git_seconds', [10, 130])
async def test_installer_uses_original_deadline_policy_limits_and_private_host_scratch(tmp_path, monkeypatch, git_seconds):
    import asyncio
    import subprocess

    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt

    cache = runtime_cache(tmp_path/'cache')
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=120, now=1000)
    cache.environment_attempt['status'] = 'executing'
    source = tmp_path/'source'
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    (source/'payload').write_text('pinned bytes')
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    snapshot = harness.Snapshot(source, commit, 'fixture', 'sym')
    target = tmp_path/'case'
    target.mkdir()
    clock = [1001.0]
    monkeypatch.setattr(harness.time, 'time', lambda: clock[0])
    real_wait = harness._bounded_wait
    async def git_wait(proc, timeout):
        value = await real_wait(proc, timeout)
        clock[0] += git_seconds
        return value
    monkeypatch.setattr(harness, '_bounded_wait', git_wait)
    real_spawn = asyncio.create_subprocess_exec
    seen = {}
    waits = []
    real_wait_for = asyncio.wait_for
    async def bounded_wait(awaitable, timeout):
        waits.append(timeout)
        return await real_wait_for(awaitable, timeout)
    monkeypatch.setattr(harness.asyncio, 'wait_for', bounded_wait)
    class Process:
        returncode = 0
        async def communicate(self, input):
            seen['input'] = input
            return b'{}', b''
    async def spawn(*args, **kwargs):
        if args[0] != 'docker':
            return await real_spawn(*args, **kwargs)
        seen['command'] = args
        return Process()
    monkeypatch.setattr(harness.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(harness, '_remove_container', AsyncMock(return_value=True))
    if git_seconds == 130:
        with pytest.raises(TimeoutError):
            await harness._install(snapshot, cache, cache.environment_capabilities['image_ref'], target, 900)
        assert 'command' not in seen
        assert not (target/'installer-source/payload').exists()
        return
    await harness._install(snapshot, cache, cache.environment_capabilities['image_ref'], target, 900)
    command = seen['command']
    assert 'COORDINARE_ENV_RUNTIME_EXPIRES_AT=1120.0' in command
    assert command[command.index('--cap-drop')+1] == 'ALL'
    assert command[command.index('--security-opt')+1] == 'no-new-privileges'
    assert command[command.index('--memory')+1] == '4096m'
    assert command[command.index('--memory-swap')+1] == '4096m'
    assert any(str(target) in item and item.endswith(':/tmp') for item in command)
    assert not any('COORDINARE_ENV_RUNTIME_ARTIFACTS=' in item for item in command)
    assert 0 < waits[-1] <= 99


@pytest.mark.parametrize('problem', [None, 'mutable_image', 'oversized', 'unknown_field'])
def test_field_runtime_policy_is_bounded_and_validated_before_use(tmp_path, problem):
    from tests.unit.test_353_runtime_identity import policy_data

    data = policy_data()
    if problem == 'mutable_image':
        data['identity']['carrier_image'] = 'performer:latest'
    elif problem == 'unknown_field':
        data['privileged'] = True
    path = tmp_path/'runtime-policy.json'
    path.write_text(' '*20000 if problem == 'oversized' else json.dumps(data))
    if problem is None:
        loaded = harness.load_runtime_policy(path)
        assert loaded['identity']['carrier_image'] == 'registry.example/performer@sha256:'+'a'*64
        assert loaded['resources']['max_seconds'] == 1800
    else:
        with pytest.raises(ValueError):
            harness.load_runtime_policy(path)


@pytest.mark.asyncio
async def test_installer_refuses_image_drift_before_creating_any_process(tmp_path, monkeypatch):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt

    cache = runtime_cache(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=120)
    cache.environment_attempt['status'] = 'executing'
    spawn = AsyncMock(side_effect=AssertionError('image drift reached a process'))
    monkeypatch.setattr(harness.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(ValueError, match='image'):
        await harness._install(SimpleNamespace(source=tmp_path, commit=cache.bootstrap_commit_sha), cache,
            'performer:latest', tmp_path, 900)
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_field_validation_records_failed_installer_before_success_receipt_gate(tmp_path, monkeypatch):
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import (
        begin_attempt,
        record_execution_failure,
        save_attempt,
    )

    cache = runtime_cache(tmp_path)
    manifest = accepted_environment(cache)
    report = runtime_report(cache, phase='installer')
    report['runtime_artifacts'] = None
    report['results'][0].update(status='fail', exit_code=2)
    report['results'][1].update(status='not_run', exit_code=None)
    snapshot = SimpleNamespace(name='sym', commit=manifest.source_commit, verify=AsyncMock())
    image = cache.environment_capabilities['image_ref']
    (tmp_path/'execution-approved.json').write_text(json.dumps({'scope': 'environment_validation',
        'approved': True, 'manifest_digest': manifest.digest(), 'source_commit': snapshot.commit, 'image_ref': image}))
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    save_attempt(cache, budget, executing=True)
    expires = cache.environment_attempt['expires_at']
    monkeypatch.setattr(harness, '_install', AsyncMock(return_value=(1, json.dumps(report).encode())))
    verifier = AsyncMock(side_effect=AssertionError('failed installer cannot launch a consumer'))
    monkeypatch.setattr(harness, 'verify_env_cache_clean', verifier)
    def record(name, state, evidence, *, cleanup_confirmed):
        assert record_execution_failure(state['env_cache'][name], evidence, cleanup_confirmed=cleanup_confirmed)
    service = Mock()
    service.record_bootstrap_execution.side_effect = record
    result = {}
    await harness.execute_accepted(snapshot, service, {'env_cache': {'sym': cache}}, image=image,
        target=tmp_path, profile=harness.Profile(model='fixture', base_url='http://localhost:1'), result=result)
    verifier.assert_not_awaited()
    assert result['reason_code'] == 'installer_failed' and not result['installer_passed']
    assert cache.environment_attempt['execution_repairs'] == 1
    assert cache.environment_attempt['expires_at'] == expires
    assert cache.environment_attempt['rejected'][0]['report']['runtime_artifacts'] is None
    assert not cache.cache_dir_ready


@pytest.mark.asyncio
async def test_an_installer_that_crashes_before_reporting_is_held_with_a_redacted_tail(tmp_path, monkeypatch):
    """v30: the private runtime guest exited, the installer printed a traceback, and the stage failed with nothing kept."""
    cache = runtime_cache(tmp_path)
    manifest = accepted_environment(cache)
    snapshot = SimpleNamespace(name='sym', commit=manifest.source_commit, verify=AsyncMock())
    image = cache.environment_capabilities['image_ref']
    (tmp_path/'execution-approved.json').write_text(json.dumps({'scope': 'environment_validation',
        'approved': True, 'manifest_digest': manifest.digest(), 'source_commit': snapshot.commit, 'image_ref': image}))
    crash = (b'Traceback (most recent call last):\n  ...\nRuntimeError: private runtime guest exited unexpectedly '
             b'(exit 137); console tail: Out of memory: Killed process 4242 | token=ghp_' + b'a' * 36 + b'\n')
    monkeypatch.setattr(harness, '_install', AsyncMock(return_value=(1, crash)))
    verifier = AsyncMock()
    monkeypatch.setattr(harness, 'verify_env_cache_clean', verifier)
    service = Mock()
    result = {}
    await harness.execute_accepted(snapshot, service, {'env_cache': {'sym': cache}}, image=image,
        target=tmp_path, profile=harness.Profile(model='fixture', base_url='http://localhost:1'), result=result)
    assert (result['status'], result['reason_code']) == ('held', 'installer_crashed')
    assert 'Out of memory' in result['installer_output_tail']
    assert 'ghp_' + 'a' * 36 not in result['installer_output_tail']
    verifier.assert_not_awaited()
    service.on_bootstrap_complete.assert_called_once_with('sym', False, {'env_cache': {'sym': cache}})


def test_the_guest_console_tail_is_bounded_and_printable(tmp_path):
    from coordinare_service_inference.environment.runtime_supervisor import (
        CONSOLE_TAIL_BYTES,
        console_tail,
    )

    console = tmp_path/'serial.log'
    console.write_bytes(b'boot\n' * 400 + b'\x1b[0m[  812.1] Out of memory: Killed process 4242 (ruby)\r\n\x00')
    tail = console_tail(console)
    assert tail.endswith('Out of memory: Killed process 4242 (ruby)')
    assert '\x1b' not in tail and '\x00' not in tail
    assert len(tail) <= CONSOLE_TAIL_BYTES
    assert console_tail(tmp_path/'absent.log') == ''


@pytest.mark.asyncio
async def test_an_installer_that_outlives_its_deadline_is_held_as_a_timeout(tmp_path, monkeypatch):
    """v39: the install outlived the guest's 30-minute lifetime and the stage failed with nothing recorded."""
    cache = runtime_cache(tmp_path)
    manifest = accepted_environment(cache)
    snapshot = SimpleNamespace(name='sym', commit=manifest.source_commit, verify=AsyncMock())
    image = cache.environment_capabilities['image_ref']
    (tmp_path/'execution-approved.json').write_text(json.dumps({'scope': 'environment_validation',
        'approved': True, 'manifest_digest': manifest.digest(), 'source_commit': snapshot.commit, 'image_ref': image}))
    monkeypatch.setattr(harness, '_install', AsyncMock(side_effect=TimeoutError('installer original deadline exhausted')))
    service = Mock()
    result = {}
    await harness.execute_accepted(snapshot, service, {'env_cache': {'sym': cache}}, image=image,
        target=tmp_path, profile=harness.Profile(model='fixture', base_url='http://localhost:1'), result=result)
    assert (result['status'], result['reason_code']) == ('held', 'installer_timeout')
    assert result['installer_seconds'] >= 0
    service.on_bootstrap_complete.assert_called_once_with('sym', False, {'env_cache': {'sym': cache}})


@pytest.mark.asyncio
async def test_an_unconfirmed_teardown_holds_and_keeps_its_command_evidence(tmp_path, monkeypatch):
    """d2: every command ran, then teardown was not clean; the run must hold, never repair, and keep the evidence."""
    cache = runtime_cache(tmp_path)
    manifest = accepted_environment(cache)
    snapshot = SimpleNamespace(name='sym', commit=manifest.source_commit, verify=AsyncMock())
    image = cache.environment_capabilities['image_ref']
    (tmp_path/'execution-approved.json').write_text(json.dumps({'scope': 'environment_validation',
        'approved': True, 'manifest_digest': manifest.digest(), 'source_commit': snapshot.commit, 'image_ref': image}))
    report = {'manifest_digest': manifest.digest(), 'platform': 'linux/aarch64', 'scope': '',
              'runtime_digest': manifest.runtime.digest(), 'runtime_phase': 'installer', 'runtime_cleanup': False,
              'runtime_teardown_failures': ['clean_shutdown'],
              'results': [{'id': 'install-one', 'status': 'pass', 'required': True, 'duration_seconds': 12.5},
                          {'id': 'check-one', 'status': 'fail', 'required': True, 'duration_seconds': 3.0}]}
    monkeypatch.setattr(harness, '_install', AsyncMock(return_value=(1, json.dumps(report).encode())))
    verifier = AsyncMock()
    monkeypatch.setattr(harness, 'verify_env_cache_clean', verifier)
    service = Mock()
    result = {}
    await harness.execute_accepted(snapshot, service, {'env_cache': {'sym': cache}}, image=image,
        target=tmp_path, profile=harness.Profile(model='fixture', base_url='http://localhost:1'), result=result)
    assert (result['status'], result['reason_code']) == ('held', 'runtime_teardown_unconfirmed')
    assert result['runtime_teardown_failures'] == ['clean_shutdown']
    assert result['installer_result_counts'] == {'pass': 1, 'fail': 1}
    assert result['installer_failed_ids'] == ['check-one']
    assert json.loads((tmp_path/'installer-output.json').read_text())['runtime_cleanup'] is False
    verifier.assert_not_awaited()
    service.record_bootstrap_execution.assert_not_called()
    service.on_bootstrap_complete.assert_called_once_with('sym', False, {'env_cache': {'sym': cache}})

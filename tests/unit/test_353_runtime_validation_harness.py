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

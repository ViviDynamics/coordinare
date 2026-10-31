"""Private runtime ordering: real manifest commands with hardware/SSH endpoints replaced.

No Docker, guest, model, credentials, or acceptance publication.
The real run_manifest, execute_with_runtime, open_runtime readiness loop,
manifest_services, _commands, and _execute functions remain in use.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.performer.tests.source_contract_fixture import current_manifest_fixture
from coordinare_service_inference.environment import runtime_execution, runtime_supervisor
from coordinare_service_inference.environment.runner import run_manifest
from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt
from coordinare_service_inference.environment.runtime_bundle import RuntimeBundleInputs
from coordinare_service_inference.environment.runtime_guest import cloud_configuration

from tests.unit.env_manifest_fixture import reviewed_metadata
from tests.unit.test_353_runtime_supervisor import runtime_spec


def command(identifier, phase, *, failure=False):
    body = (
        'import os,sys;from pathlib import Path;'
        'assert os.environ["DOCKER_HOST"].startswith("ssh://root@127.0.0.1:");'
        'p=Path("{repo}/events");assert "guest-ready" in p.read_text().splitlines();'
        f'p.open("a").write("{phase}\\n");'
        f'sys.exit({7 if failure else 0})'
    )
    return {'id': identifier, 'argv': [sys.executable, '-c', body], 'cwd': '{repo}',
            'timeout_seconds': 3, 'required': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('install', [True, False], ids=['installer', 'offline-consumer'])
@pytest.mark.parametrize('health_fails', [False, True], ids=['healthy', 'health-failure'])
async def test_real_production_boundaries_wait_then_install_services_checks_cleanup(tmp_path, monkeypatch, install, health_fails):
    await exercise(tmp_path, monkeypatch, install=install, health_fails=health_fails)


@pytest.mark.asyncio
async def test_not_ready_guest_never_reaches_any_project_command(tmp_path, monkeypatch):
    await exercise(tmp_path, monkeypatch, install=True, health_fails=False, never_ready=True)


async def exercise(tmp_path, monkeypatch, *, install, health_fails, never_ready=False):
    cache, repo, scratch = (tmp_path / name for name in ('cache', 'repo', 'scratch'))
    for path in (cache, repo, scratch):
        path.mkdir()
    events = repo / 'events'
    events.write_text('')
    def event(text):
        with events.open('a') as stream:
            stream.write(text + '\n')
    spec = runtime_spec()
    data = reviewed_metadata()['environment_manifest']
    manifest = current_manifest_fixture({**data, 'runtime': spec.model_dump(mode='json'),
        'requirements': [], 'activation': {'path': [], 'env': {}},
        'install': [command('install-probe', 'install')],
        'checks': [command('check-probe', 'check')],
        'services': [{'id': 'service-probe', 'required': True,
            'init': command('init-probe', 'init'), 'start': command('start-probe', 'start'),
            'health': command('health-probe', 'health', failure=health_fails),
            'stop': command('stop-probe', 'stop')}]})
    artifact = cache / 'installed.qcow2'
    artifact.write_bytes(b'runtime-order-fixture')
    receipt = RuntimeArtifactReceipt(schema_version=1, manifest_digest=manifest.digest(), source_digest='2'*64,
        runtime_digest=spec.digest(), sealed=True, artifacts=[{'path': artifact.name,
            'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'bytes': artifact.stat().st_size}])
    bundle = RuntimeBundleInputs(schema_version=1, guest_platform='linux/x86_64',
        guest_image='/offline-fixture/base', emulator='/offline-fixture/qemu', firmware='/offline-fixture/firmware')
    monkeypatch.setattr(runtime_supervisor, '_admit', lambda *args, **kwargs: bundle)
    monkeypatch.setattr(runtime_supervisor.RuntimeLease, 'check_bounds', lambda *args, **kwargs: None)
    async def platform_command(argv, **kwargs):
        assert Path(argv[0]).name == 'qemu-img'
        if 'info' in argv:
            return 0, b'{"format":"qcow2","virtual-size":4096}'
        assert 'create' in argv
        return 0, b''
    monkeypatch.setattr(runtime_supervisor, '_command', platform_command)
    async def control(lease, **kwargs):
        (lease.work_root / 'bin').mkdir()
        (lease.work_root / 'docker').mkdir()
        return ['TEST_GUEST_PROCESS_BOUNDARY']
    monkeypatch.setattr(runtime_supervisor, '_prepare_control', control)
    actual_spawn = asyncio.create_subprocess_exec
    async def spawn(*argv, **kwargs):
        if argv[0] == 'TEST_GUEST_PROCESS_BOUNDARY':
            event('guest-created')
            return SimpleNamespace(returncode=None)
        assert argv[0] == sys.executable, 'Only Python marker subprocesses permitted'
        return await actual_spawn(*argv, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    polls = []
    readiness_clock=[time.monotonic()]
    if never_ready:
        monkeypatch.setattr(runtime_supervisor,'time',SimpleNamespace(monotonic=lambda:readiness_clock[0]))
    async def ssh(lease, text, **kwargs):
        if text == runtime_execution.RESIDUAL_LIST:
            return 0, b'containers=\nvolumes=\nnetworks=\n'
        assert text == 'test -f /run/coordinare-runtime-ready && docker info >/dev/null'
        polls.append(text)
        if never_ready or len(polls) < 2:
            event('guest-not-ready')
            if never_ready:
                readiness_clock[0]=lease.deadline
                raise TimeoutError('Synthetic readiness transport deadline')
            return 1, b''
        event('guest-ready')
        return 0, b''
    monkeypatch.setattr(runtime_supervisor.RuntimeLease, 'ssh', ssh)
    async def close(lease, **kwargs):
        event('runtime-close')
        shutil.rmtree(lease.work_root)
        return SimpleNamespace(confirmed=True, clean_shutdown=True, bounds_respected=True)
    monkeypatch.setattr(runtime_supervisor.RuntimeLease, 'close', close)
    async def seal(lease, **kwargs):
        assert lease.installation_passed
        event('seal-boundary')
        return receipt
    monkeypatch.setattr(runtime_execution, 'seal_runtime', seal)
    kwargs = {'cache':cache,'repo':repo,'scratch':scratch,'install':install,
        'execution_source_digest':'2'*64,'runtime_artifacts':None if install else receipt,
        'runtime_deadline':time.monotonic()+15,'credentials':{}}
    if never_ready:
        with pytest.raises(TimeoutError):
            await run_manifest(manifest, **kwargs)
        assert events.read_text().splitlines() == ['guest-created','guest-not-ready','runtime-close']
        return
    report = await run_manifest(manifest, **kwargs)
    expected = ['guest-created', 'guest-not-ready', 'guest-ready']
    if install:
        expected.append('install')
    expected += ['init', 'start', 'health']
    if not health_fails:
        expected.append('check')
    expected.append('stop')
    if install and not health_fails:
        expected.append('seal-boundary')
    expected.append('runtime-close')
    assert events.read_text().splitlines() == expected
    assert report.passed is not health_fails
    assert report.runtime_cleanup is True
    assert next(x for x in report.results if x.id == 'check-probe').status == ('not_run' if health_fails else 'pass')
    assert len(polls) == 2


def test_real_guest_boot_config_starts_daemon_and_waits_before_ready_marker(tmp_path):
    config = json.loads(cloud_configuration(repo=tmp_path/'repo', cache=tmp_path/'cache',
        scratch=tmp_path/'scratch', readonly=True).split('\n', 1)[1])
    boot = config['runcmd'][0]
    assert boot.index('rc-service docker start') < boot.index('docker info >/dev/null') < boot.index('touch /run/coordinare-runtime-ready')
    assert boot.startswith('#!/bin/sh\nset -eu\n')

"""Probe execution confines project commands to a disposable consumer."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_353_environment_probes import plan


def test_probe_container_has_no_network_writable_source_or_credentials(tmp_path):
    from coordinare.services.environment_probe_executor import probe_container_argv
    args = probe_container_argv(plan(), tmp_path/'repo', tmp_path/'cache')
    assert '--network=none' in args
    assert '--read-only' in args
    assert '--cap-drop=ALL' in args
    assert all('readonly' in args[i+1] for i, arg in enumerate(args) if arg == '--mount')
    assert not any('socket' in arg or '--env' in arg for arg in args)
    assert plan().capabilities.image_ref in args


@pytest.mark.asyncio
async def test_unsupported_platform_holds_before_execution():
    from coordinare.services.environment_probe_executor import execute_environment_probe
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await execute_environment_probe(plan(), state={'probe_backend':'kubernetes'}, source_files={}, budget=AttemptBudget())
    assert exc.value.code == 'probe_isolation_unsupported'


def test_probe_supervisor_bounds_output_and_timeout():
    from coordinare.services import environment_probe_runtime
    code = Path(environment_probe_runtime.__file__).read_text()
    commands = [{'id':'large', 'argv':[sys.executable, '-c', "print('x'*1000000)"], 'cwd':'/tmp',
        'env':{}, 'timeout_seconds':1}, {'id':'slow', 'argv':[sys.executable, '-c', 'import time;time.sleep(5)'],
        'cwd':'/tmp', 'env':{}, 'timeout_seconds':0.1}]
    result = subprocess.run([sys.executable, '-I', '-c', code], input=json.dumps({'commands':commands, 'seconds':2}),
        capture_output=True, text=True, timeout=4)
    assert result.returncode == 0, result.stderr
    results = json.loads(result.stdout)
    assert results[0]['truncated'] and len(results[0]['output'].encode()) <= 16384
    assert results[1]['status'] == 'timed_out'


@pytest.mark.asyncio
async def test_probe_source_drift_never_creates_container():
    from coordinare.services.environment_probe_executor import execute_environment_probe
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await execute_environment_probe(plan(), state={'probe_backend':'docker'}, source_files={}, budget=AttemptBudget())
    assert exc.value.code == 'probe_source_identity'


def test_probe_supervisor_kills_descendants_holding_output():
    from coordinare.services import environment_probe_runtime
    code = Path(environment_probe_runtime.__file__).read_text()
    command = {'id':'fork', 'argv':[sys.executable, '-c',
        'import subprocess,sys;subprocess.Popen([sys.executable,"-c","import time;time.sleep(20)"]);print("parent done")'],
        'cwd':'/tmp', 'env':{}, 'timeout_seconds':0.2}
    result = subprocess.run([sys.executable, '-I', '-c', code], input=json.dumps({'commands':[command], 'seconds':1}),
        capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert json.loads(result.stdout)[0]['status'] == 'timed_out'


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup', [True, False])
async def test_cancelled_late_create_is_drained_before_cleanup(monkeypatch, cleanup):
    import hashlib

    from coordinare.services import environment_probe_executor as executor
    from tests.unit.test_353_discovery_repair import PATH, SOURCE
    p = plan().model_copy(update={'inputs':{PATH:hashlib.sha256(SOURCE.encode()).hexdigest()}})
    entered, release = asyncio.Event(), asyncio.Event()
    order = []
    async def identity(image):
        return image, p.capabilities.platform
    async def docker(args, **kwargs):
        entered.set()
        await release.wait()
        order.append('created')
        return 0, b''
    async def remove(name):
        order.append('removed')
        return cleanup
    monkeypatch.setattr(executor, '_docker_identity', identity)
    monkeypatch.setattr(executor, '_docker', docker)
    monkeypatch.setattr(executor, '_remove', remove)
    task = asyncio.create_task(executor.execute_environment_probe(p, state={'probe_backend':'docker'},
        source_files={PATH:SOURCE}, budget=AttemptBudget()))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError if cleanup else EnvironmentDiscoveryError):
        await task
    assert order == ['created', 'removed']

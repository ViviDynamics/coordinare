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
from tests.unit.test_353_execution_source_pipeline import bundle_fixture


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
async def test_unsupported_platform_holds_before_execution(tmp_path):
    from coordinare.services.environment_probe_executor import execute_environment_probe
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await execute_environment_probe(plan(), state={'probe_backend':'kubernetes'}, source_bundle=bundle_fixture(tmp_path), budget=AttemptBudget())
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
async def test_probe_source_drift_never_creates_container(tmp_path):
    from coordinare.services.environment_probe_executor import execute_environment_probe
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await execute_environment_probe(plan(), state={'probe_backend':'docker'}, source_bundle=bundle_fixture(tmp_path), budget=AttemptBudget())
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
async def test_cancelled_late_create_is_drained_before_cleanup(monkeypatch, cleanup, tmp_path):
    import hashlib

    from coordinare.services import environment_probe_executor as executor
    from tests.unit.test_353_discovery_repair import PATH, SOURCE
    bundle = bundle_fixture(tmp_path, {PATH:(SOURCE.encode(), '100644')})
    p = plan().model_copy(update={'inputs':{PATH:hashlib.sha256(SOURCE.encode()).hexdigest()},
        'execution_source_digest':bundle.identity.digest()})
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
        source_bundle=bundle, budget=AttemptBudget()))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError if cleanup else EnvironmentDiscoveryError):
        await task
    assert order == ['created', 'removed']


@pytest.mark.asyncio
async def test_failed_create_still_attempts_removal(monkeypatch, tmp_path):
    import hashlib

    from coordinare.services import environment_probe_executor as executor
    from tests.unit.test_353_discovery_repair import PATH, SOURCE
    bundle = bundle_fixture(tmp_path, {PATH:(SOURCE.encode(), '100644')})
    p = plan().model_copy(update={'inputs':{PATH:hashlib.sha256(SOURCE.encode()).hexdigest()},
        'execution_source_digest':bundle.identity.digest()})
    removed = []
    async def identity(image):
        return image, p.capabilities.platform
    async def docker(*args, **kwargs):
        raise TimeoutError
    async def remove(name):
        removed.append(name)
        return True
    monkeypatch.setattr(executor, '_docker_identity', identity)
    monkeypatch.setattr(executor, '_docker', docker)
    monkeypatch.setattr(executor, '_remove', remove)
    with pytest.raises(TimeoutError):
        await executor.execute_environment_probe(p, state={'probe_backend':'docker'}, source_bundle=bundle, budget=AttemptBudget())
    assert len(removed) == 1


def test_detached_child_cannot_keep_supervisor_past_deadline(tmp_path):
    import os
    import signal
    import time

    from coordinare.services import environment_probe_runtime
    child_pid = tmp_path/'child-pid'
    script = ('import subprocess,sys;'
        'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(10)"],start_new_session=True);'
        f'open({str(child_pid)!r},"w").write(str(p.pid));import time;time.sleep(3)')
    command = {'id':'detached','argv':[sys.executable,'-c',script], 'cwd':'/tmp','env':{},'timeout_seconds':0.1}
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable,'-I','-c',Path(environment_probe_runtime.__file__).read_text()],
            input=json.dumps({'commands':[command],'seconds':0.2}),capture_output=True,text=True,timeout=3)
        assert result.returncode == 0
        assert time.monotonic() - started < 1
        assert json.loads(result.stdout)[0]['status'] == 'timed_out'
    finally:
        if child_pid.exists():
            os.kill(int(child_pid.read_text()), signal.SIGKILL)


def test_probe_expands_environment_references_and_rejects_unknown_secrets():
    from coordinare.services import environment_probe_runtime
    commands = [{'id':'env','argv':[sys.executable,'-c','import os;print(os.environ["PROBE_PATH"])'],
        'cwd':'/tmp','env':{'PROBE_PATH':'${PATH}'},'timeout_seconds':1},
        {'id':'secret','argv':['${SECRET_TOKEN}'],'cwd':'/tmp','env':{},'timeout_seconds':1}]
    result = subprocess.run([sys.executable,'-I','-c',Path(environment_probe_runtime.__file__).read_text()],
        input=json.dumps({'commands':commands,'seconds':3}),capture_output=True,text=True,timeout=4)
    assert result.returncode == 0
    results = json.loads(result.stdout)
    assert '${PATH}' not in results[0]['output']
    assert '/' in results[0]['output']
    assert results[1]['status'] == 'execution_error'


@pytest.mark.asyncio
async def test_host_collection_is_bounded_before_report_decoding():
    from coordinare.services.environment_probe_executor import _docker
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await _docker([sys.executable,'-c',"print('x'*1000000)"])
    assert exc.value.code == 'probe_output_limit'


@pytest.mark.asyncio
async def test_cleanup_is_drained_when_its_wait_is_cancelled(monkeypatch, tmp_path):
    import hashlib

    from coordinare.services import environment_probe_executor as executor
    from tests.unit.test_353_discovery_repair import PATH, SOURCE
    bundle = bundle_fixture(tmp_path, {PATH:(SOURCE.encode(), '100644')})
    p = plan().model_copy(update={'inputs':{PATH:hashlib.sha256(SOURCE.encode()).hexdigest()},
        'execution_source_digest':bundle.identity.digest()})
    cleaning, release = asyncio.Event(), asyncio.Event()
    removed = []
    async def identity(image):
        return image, p.capabilities.platform
    async def docker(args, **kwargs):
        return 0, b'[{"id":"tool","status":"completed","exit_code":127,"output":"absent","truncated":false}]'
    async def remove(name):
        cleaning.set()
        await release.wait()
        removed.append(name)
        return True
    monkeypatch.setattr(executor, '_docker_identity', identity)
    monkeypatch.setattr(executor, '_docker', docker)
    monkeypatch.setattr(executor, '_remove', remove)
    task = asyncio.create_task(executor.execute_environment_probe(p, state={'probe_backend':'docker'},
        source_bundle=bundle, budget=AttemptBudget()))
    await cleaning.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(removed) == 1

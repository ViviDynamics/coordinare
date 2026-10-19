"""Resource failures after verification cancel ordinary work and cannot be waived."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.runtime_supervisor import RuntimeCleanupReport
from performer import environment_runtime, main
from performer.backends.base import BackendStatus
from performer.environment_contract import prepare_environment
from performer.models import Performance, Stand
from performer.protocol import PerformerResponse

from .test_353_environment_runtime import private_score


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['http', 'stdio'])
async def test_late_resource_failure_stops_real_backend_before_success(tmp_path, monkeypatch, transport):
    score, manifest, source = private_score(tmp_path)
    stand = Stand(path=tmp_path, branch='main')
    marker, key = tmp_path/'limit-exceeded', tmp_path/'control-key'
    process = None
    closed = False

    def check_bounds(*, stop=None):
        if marker.exists():
            marker.unlink()  # A later clean measurement cannot waive this failure.
            raise RuntimeError('private runtime cache ceiling exhausted')

    async def close(*, deadline):
        nonlocal closed
        key.unlink(missing_ok=True)
        closed = True
        return RuntimeCleanupReport(True, True, True, True, True)

    async def open_runtime(*args, **kwargs):
        key.write_text('ephemeral fixture')
        return SimpleNamespace(spec=manifest.runtime, manifest_digest=manifest.digest(), source_digest=source.digest(),
            install=False, deadline=time.monotonic()+10, scratch=tmp_path, close=close, check_bounds=check_bounds,
            activation_env={'DOCKER_HOST': 'ssh://private-fixture', 'DOCKER_CONFIG': '/owned/config',
                            'DOCKER_CONTEXT': '', 'PATH': os.environ['PATH']})

    class Backend:
        def get_status(self):
            return BackendStatus(state='working')
        def drain_events(self):
            return []
        async def stop(self):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    backend = Backend()
    perf = Performance(session_id='monitor', stand=stand, score=score, backend=backend, state='working')

    async def start():
        nonlocal process
        await prepare_environment(score, stand)
        process = await asyncio.create_subprocess_exec(sys.executable, '-c',
            'import sys,time;from pathlib import Path;time.sleep(.1);Path(sys.argv[1]).write_text("exceeded");time.sleep(60)',
            str(marker), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    async def job():
        await start()
        return await main._poll_job_status(perf, None)

    async def dispatch(*args):
        await start()
        return PerformerResponse(status='accepted', session_id='monitor'), perf

    monkeypatch.setattr(environment_runtime, 'open_runtime', open_runtime)
    monkeypatch.setattr(main, 'cleanup_stand', lambda stand: None)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, 'r') as reader:
        monkeypatch.setattr(main.sys, 'stdin', reader)
        monkeypatch.setattr(main, '_dispatch_action', dispatch)
        monkeypatch.setattr(main, '_init_metrics', lambda: None)
        monkeypatch.setattr(main, 'get_settings', lambda: SimpleNamespace(AGENT_TIMEOUT=30))
        os.write(write_fd, (json.dumps({'action': 'dispatch', 'payload': {}})+'\n').encode())
        try:
            with pytest.raises(ValueError, match='runtime.*monitor'):
                await asyncio.wait_for(job() if transport == 'http' else main.run_loop(), timeout=3)
            assert process is not None and process.returncode is not None
            assert closed and not key.exists()
            assert stand.environment_runtime is None
        finally:
            os.close(write_fd)
            await backend.stop()
            from performer.environment_contract import close_environment
            with contextlib.suppress(ValueError):
                await close_environment(stand)


@pytest.mark.asyncio
async def test_installer_stops_monitor_before_removing_sealed_runtime(tmp_path, monkeypatch):
    from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt
    from performer import environment_contract

    score, manifest, source = private_score(tmp_path)
    score.role = 'env_bootstrap'
    score.environment_attempt_expires_at = time.time()+10
    stand = Stand(path=tmp_path, branch='main')
    closed = False

    def check_bounds(*, stop=None):
        if closed:
            raise RuntimeError('runtime work tree has been removed')

    async def close(*, deadline):
        nonlocal closed
        first = not closed
        closed = True
        if first:
            await asyncio.sleep(0.35)
        return RuntimeCleanupReport(True, True, True, True, True)

    async def open_runtime(*args, **kwargs):
        return SimpleNamespace(spec=manifest.runtime, manifest_digest=manifest.digest(), source_digest=source.digest(),
            install=True, installation_passed=True, deadline=time.monotonic()+10, scratch=tmp_path,
            close=close, check_bounds=check_bounds, activation_env={'DOCKER_HOST': 'ssh://private-fixture',
            'DOCKER_CONFIG': '/owned/config', 'DOCKER_CONTEXT': '', 'PATH': os.environ['PATH']})

    async def seal(*args, **kwargs):
        return RuntimeArtifactReceipt.model_validate(score.environment_runtime_artifacts)

    monkeypatch.setattr(environment_runtime, 'open_runtime', open_runtime)
    monkeypatch.setattr(environment_contract, 'seal_runtime', seal)
    await prepare_environment(score, stand)
    try:
        result = await environment_contract.execute_contract(score, stand)
        assert result['passed'] and result['environment_verification']['runtime_cleanup']
        assert stand.environment_runtime is None
        await asyncio.sleep(0.3)
    finally:
        with contextlib.suppress(ValueError):
            await environment_contract.close_environment(stand)


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_owned_service_cleanup(tmp_path):
    from contextlib import AsyncExitStack
    from performer.environment_contract import close_environment

    key = tmp_path/'owned-key'
    key.touch()
    started, finish = asyncio.Event(), asyncio.Event()
    lifecycle = AsyncExitStack()

    async def stop_service():
        started.set()
        await finish.wait()
        key.unlink()

    lifecycle.push_async_callback(stop_service)
    stand = Stand(path=tmp_path, branch='main', environment_lifecycle=lifecycle)
    task = asyncio.create_task(close_environment(stand))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert not key.exists()
        await close_environment(stand)
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)

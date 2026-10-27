"""Service cancellation owns a process even before its handle reaches the caller."""
from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from pathlib import Path

import pytest
from performer import workspace


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['start', 'health'])
@pytest.mark.parametrize('cancel_twice', [False, True], ids=['cancel', 'repeated-cancel'])
async def test_service_spawn_is_drained_before_cancellation_returns(tmp_path, monkeypatch, phase, cancel_twice):
    cache = tmp_path / 'sample-cache'
    services = cache / 'services'
    services.mkdir(parents=True)
    child_ready, release_child, late = (cache / name for name in ['child-ready', 'release-child', 'late'])
    script = services / f'services-{phase}.sh'
    script.write_text(
        '#!/bin/bash\n'
        f'(touch "{child_ready}"; while [ ! -f "{release_child}" ]; do sleep 0.01; done; touch "{late}") &\n'
        'exec sleep 60\n',
    )
    script.chmod(0o755)
    monkeypatch.setattr(workspace, 'frozen_scripts_dir', lambda value: Path(value))
    actual_spawn = asyncio.create_subprocess_exec
    created, publish_handle = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_publication(*args, **kwargs):
        process = await actual_spawn(*args, **kwargs)
        processes.append(process)
        created.set()
        await publish_handle.wait()
        return process

    monkeypatch.setattr(workspace.asyncio, 'create_subprocess_exec', delayed_publication)
    run = workspace._start_env_cache_services if phase == 'start' else workspace._run_env_cache_health_check
    task = asyncio.create_task(run(str(cache), {}))
    try:
        await asyncio.wait_for(created.wait(), 5)

        async def wait_for_descendant():
            while not child_ready.exists():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_descendant(), 5)
        task.cancel()
        await asyncio.sleep(0)
        if cancel_twice:
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done(), 'cancellation cannot return before the owned spawn is reconciled'
        publish_handle.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        for process in processes:
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)
        release_child.touch()
        await asyncio.sleep(0.1)
        assert not late.exists()
    finally:
        publish_handle.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for process in processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize('channels', [(1,), (2,), (1, 2)], ids=['stdout', 'stderr', 'both-pipes'])
async def test_cancelled_health_spawn_drains_paused_pipes_before_return(tmp_path, monkeypatch, channels):
    import shlex
    import sys

    cache = tmp_path / 'noisy-sample-cache'
    services = cache / 'services'
    services.mkdir(parents=True)
    script = services / 'services-health.sh'
    program = (
        'import os,time,threading;'
        f'channels={channels!r};'
        '[threading.Thread(target=os.write,args=(fd,b"x"*1048576),daemon=True).start() for fd in channels];'
        'time.sleep(60)'
    )
    script.write_text(f'#!/bin/bash\nexec {shlex.quote(sys.executable)} -c {shlex.quote(program)}\n')
    script.chmod(0o755)
    monkeypatch.setattr(workspace, 'frozen_scripts_dir', lambda value: Path(value))
    actual_spawn = asyncio.create_subprocess_exec
    created, publish_handle = asyncio.Event(), asyncio.Event()
    processes = []

    async def paused_pipe_spawn(*args, **kwargs):
        process = await actual_spawn(*args, **kwargs)
        processes.append(process)

        async def wait_for_backpressure():
            streams = {1: process.stdout, 2: process.stderr}
            # Force the actual standard StreamReader high-water boundary before cancellation.
            while not all(streams[fd]._paused for fd in channels):
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_backpressure(), 5)
        created.set()
        await publish_handle.wait()
        return process

    monkeypatch.setattr(workspace.asyncio, 'create_subprocess_exec', paused_pipe_spawn)
    task = asyncio.create_task(workspace._run_env_cache_health_check(str(cache), {}))
    try:
        await asyncio.wait_for(created.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        publish_handle.set()
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done, 'cancellation must finish without an external pipe drainer'
        assert task.cancelled()
        for process in processes:
            assert process.returncode is not None
            assert process.stdout.at_eof() and process.stderr.at_eof()
    finally:
        publish_handle.set()
        for process in processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.communicate(), 5)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

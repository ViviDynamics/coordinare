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

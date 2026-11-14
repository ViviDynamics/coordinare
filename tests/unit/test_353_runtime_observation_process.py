"""Private observation limits cover blocked input and excessive child output."""
from __future__ import annotations

import asyncio
import contextlib
import sys
import time

import pytest
from coordinare_service_inference.environment import runtime_observation


@pytest.mark.asyncio
async def test_blocked_input_cannot_outlive_observation_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []

    async def child(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await spawn(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(runtime_observation.asyncio, 'create_subprocess_exec', child)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runtime_observation._observe_command(
                {'commands': ['x' * (1024 * 1024)]}, environment={}, deadline=started + 0.1), timeout=1)
        assert time.monotonic() - started < 0.8
        assert children[0].returncode is not None
    finally:
        for process in children:
            if process.returncode is None:
                process.kill()
            with contextlib.suppress(BrokenPipeError):
                if process.stdin is not None:
                    process.stdin.close()
            await process.communicate()


@pytest.mark.asyncio
async def test_excessive_output_is_rejected_and_child_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []

    async def child(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await spawn(sys.executable, '-c',
            "import sys,time; sys.stdin.read(); sys.stdout.buffer.write(b'x'*1048576); sys.stdout.flush(); time.sleep(60)", **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(runtime_observation.asyncio, 'create_subprocess_exec', child)
    try:
        with pytest.raises(RuntimeError, match='output exceeds bound'):
            await asyncio.wait_for(runtime_observation._observe_command(
                {'commands': []}, environment={}, deadline=time.monotonic()+3), timeout=4)
        assert children[0].returncode is not None
    finally:
        for process in children:
            if process.returncode is None:
                process.kill()
            await process.communicate()

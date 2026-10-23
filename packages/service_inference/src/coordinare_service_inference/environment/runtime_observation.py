"""Platform-only observations use a fresh guest, never installed project artifacts."""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .runtime import RuntimePolicy, RuntimeSpec
from .runtime_execution import run_guarded
from .runtime_supervisor import open_runtime


async def _observe_command(data: dict[str, Any], *, environment: dict[str, str], deadline: float) -> list[dict[str, Any]]:
    process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-m',
        'coordinare_service_inference.environment.observation',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True)
    try:
        payload = json.dumps({**data, 'runtime_environment': environment,
            'seconds': max(0, deadline-time.monotonic())}).encode()
        if process.stdin is None or process.stdout is None:
            raise RuntimeError('preflight command pipes unavailable')
        output = bytearray()
        async with asyncio.timeout_at(deadline):
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > 65536:
                    raise RuntimeError('preflight command output exceeds bound')
            await process.wait()
        if process.returncode != 0:
            raise RuntimeError('preflight command supervisor failed')
        result: list[dict[str, Any]] = json.loads(output)
        return result
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        # A full paused stdout pipe can keep wait() pending after SIGKILL.
        # Discard remaining bytes without growing the rejected report buffer.
        async with asyncio.timeout(2):
            if process.stdout is not None:
                while await process.stdout.read(8192):
                    pass
            await process.wait()


COMMAND_SECONDS = 120
TEARDOWN_SECONDS = 60


class PrivateTeardownError(RuntimeError):
    """A private guest was not confirmed clean; never reported as an ordinary probe result."""


async def observe_private(data: dict[str, Any], *, repo: Path = Path('/repo'),
                          cache: Path = Path('/cache'), scratch: Path = Path('/scratch')) -> list[dict[str, Any]]:
    policy = RuntimePolicy.model_validate(data['runtime'])
    if len(data['commands']) != 1:
        raise ValueError('private observations require one independent command per guest')
    source_digest = data.get('execution_source_digest')
    if not isinstance(source_digest, str) or len(source_digest) != 64 or any(c not in '0123456789abcdef' for c in source_digest):
        raise ValueError('private observations require complete source identity')
    remaining = data['expires_at']-time.time()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError('private preflight original deadline exhausted')
    # Teardown has its own share of the same original budget: a guest booted under load can otherwise spend the
    # whole window and leave none for a clean shutdown.
    now = time.monotonic()
    budget = min(data['seconds'], remaining, policy.resources.max_seconds, COMMAND_SECONDS + TEARDOWN_SECONDS)
    lease_deadline = now + budget
    deadline = now + min(COMMAND_SECONDS, budget - min(TEARDOWN_SECONDS, budget / 2))
    spec = RuntimeSpec(schema_version=1, policy=policy, endpoints=[])
    with tempfile.TemporaryDirectory(prefix='coordinare-project-', dir=scratch) as project:
        lease = await open_runtime(spec, artifacts=None, repo=repo, cache=cache, scratch=Path(project),
            install=False, deadline=lease_deadline, source_digest=source_digest, purpose='preflight')
        try:
            result = await run_guarded(lease, _observe_command({'commands': data['commands'],
                'roots': {'repo': str(repo), 'cache': str(cache), 'scratch': project}},
                environment=lease.activation_env, deadline=deadline))
        finally:
            cleanup = await lease.close(deadline=lease_deadline)
        if not cleanup.confirmed or not cleanup.clean_shutdown or not cleanup.bounds_respected:
            failed = [name for name in ('qemu_reaped', 'keys_absent', 'listeners_absent', 'artifacts_unchanged',
                                        'clean_shutdown', 'bounds_respected') if getattr(cleanup, name, True) is False]
            raise PrivateTeardownError('private preflight teardown is incomplete: ' + (', '.join(failed) or 'unconfirmed'))
        return result

"""Execute a manifest with one owned runtime across commands and teardown."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .runtime_supervisor import RuntimeScanStoppedError, open_runtime, seal_runtime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from .models import EnvironmentManifest, VerificationReport
    from .runtime import RuntimeArtifactReceipt
    from .runtime_supervisor import RuntimeLease


async def _settle[T](future: asyncio.Future[T]) -> T:
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    result = future.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _scan_bounds(lease: RuntimeLease) -> None:
    stop = threading.Event()
    kwargs = {"stop": stop} if "stop" in inspect.signature(lease.check_bounds).parameters else {}
    worker = asyncio.create_task(asyncio.to_thread(lease.check_bounds, **kwargs))
    try:
        await asyncio.shield(worker)
    finally:
        stop.set()
        with contextlib.suppress(RuntimeScanStoppedError):
            await _settle(worker)


async def run_guarded[T](lease: RuntimeLease, operation: Awaitable[T]) -> T:
    async def watch() -> None:
        while True:
            await _scan_bounds(lease)
            await asyncio.sleep(0.25)

    task = asyncio.ensure_future(operation)
    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait((task, watcher), timeout=max(0, lease.deadline-time.monotonic()),
                                      return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
        if task not in done:
            raise TimeoutError('private runtime execution deadline exhausted')
        return await task
    finally:
        for pending in (task, watcher):
            if not pending.done():
                pending.cancel()
        await _settle(asyncio.gather(task, watcher, return_exceptions=True))
        failure = None if watcher.cancelled() else watcher.exception()
        if failure is not None:
            raise failure


async def execute_with_runtime(
    manifest: EnvironmentManifest, *, cache: str | Path, repo: str | Path, scratch: str | Path,
    install: bool, scope: str, secret_names: Sequence[str], execution_source_digest: str | None,
    runtime_artifacts: RuntimeArtifactReceipt | None, runtime_deadline: float | None,
    runtime_lease: RuntimeLease | None, commands: Callable[..., Awaitable[VerificationReport]],
) -> VerificationReport:
    spec = manifest.runtime
    if spec is None or execution_source_digest is None:
        raise ValueError('private runtime requires accepted manifest and complete source identity')
    kwargs = {"cache": cache, "repo": repo, "scratch": scratch, "install": install, "scope": scope,
              "secret_names": secret_names, "execution_source_digest": execution_source_digest}
    if runtime_lease is not None:
        if (runtime_lease.spec != spec or runtime_lease.manifest_digest != manifest.digest()
                or runtime_lease.source_digest != execution_source_digest):
            raise ValueError('live private runtime identity differs from accepted manifest')
        runtime_lease.check_bounds()
        kwargs["scratch"] = str(runtime_lease.scratch)
        report = await run_guarded(runtime_lease, commands(manifest, **kwargs, runtime_env=runtime_lease.activation_env))
        runtime_lease.check_bounds()
        if install:
            runtime_lease.installation_passed = report.passed
        report.runtime_digest = spec.digest()
        report.runtime_phase = 'installer' if runtime_lease.install else 'live_consumer'
        return report
    deadline = runtime_deadline if runtime_deadline is not None else time.monotonic()+spec.policy.resources.max_seconds
    # The caller supplies a scratch base; export only this private project child.
    scratch_base = Path(scratch)
    parent = scratch_base if scratch_base.is_dir() else scratch_base.parent
    with tempfile.TemporaryDirectory(prefix='coordinare-project-', dir=parent) as owned_scratch:
        kwargs['scratch'] = owned_scratch
        lease = await open_runtime(spec, artifacts=runtime_artifacts, repo=Path(repo), cache=Path(cache),
            scratch=Path(owned_scratch), install=install, deadline=deadline,
            manifest_digest=manifest.digest(), source_digest=execution_source_digest)
        report = None
        receipt = runtime_artifacts
        try:
            report = await run_guarded(lease, commands(manifest, **kwargs, runtime_env=lease.activation_env))
            lease.check_bounds()
            if install and report.passed:
                lease.installation_passed = True
                receipt = await seal_runtime(lease, manifest_digest=manifest.digest(),
                                             source_digest=execution_source_digest, deadline=deadline)
        finally:
            cleanup = await lease.close(deadline=deadline)
        if not cleanup.confirmed or not cleanup.clean_shutdown or not cleanup.bounds_respected:
            raise RuntimeError('private runtime teardown or immutable artifact verification failed')
        report.runtime_digest = spec.digest()
        report.runtime_phase = 'installer' if install else 'consumer'
        report.runtime_artifacts = receipt
        report.runtime_cleanup = True
        return report

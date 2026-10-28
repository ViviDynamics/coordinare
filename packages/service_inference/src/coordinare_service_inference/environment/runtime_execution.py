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

from .runtime_io import settle as _settle
from .runtime_supervisor import RuntimeScanStoppedError, open_runtime, seal_runtime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from .models import CheckResult, EnvironmentManifest, VerificationReport
    from .runtime import RuntimeArtifactReceipt
    from .runtime_supervisor import RuntimeLease


TEARDOWN_CONDITIONS = ('qemu_reaped', 'keys_absent', 'listeners_absent', 'artifacts_unchanged', 'clean_shutdown',
                       'bounds_respected')


class PrivateRuntimeIncompleteError(RuntimeError):
    """The run cannot be accepted (deadline, seal or teardown): it holds, but keeps its evidence and the reasons."""

    def __init__(self, failed: list[str], report: VerificationReport | None) -> None:
        super().__init__('private runtime run incomplete: ' + ', '.join(failed))
        self.failed = failed
        self.report = report


DETAIL_MAX_CHARS = 600


def _cause_detail(cause: BaseException, secret_names: Sequence[str]) -> str:
    """Exception class and message, with known secret values removed; the owner applies full pattern redaction."""
    import os

    from .runner import _redact

    secrets = frozenset(os.environ[name] for name in secret_names if os.environ.get(name))
    text = _redact(f'{type(cause).__name__}: {cause}', {}, extra_values=secrets)
    if len(text) <= DETAIL_MAX_CHARS:
        return text
    # Only the owner can pattern-redact, so never cut through a word: a credential is kept whole or dropped.
    head = text[:DETAIL_MAX_CHARS]
    return head.rsplit(None, 1)[0] if any(character.isspace() for character in head) else type(cause).__name__


def _cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _partial_report(manifest: EnvironmentManifest, progress: dict[str, CheckResult], *, install: bool, scope: str,
                    execution_source_digest: str) -> VerificationReport:
    """Finished results as recorded; every declared command, service step included, without one is not_run."""
    from .models import CheckResult, VerificationReport
    from .runner import _bound_outputs

    digest = manifest.digest()
    declared = [*(manifest.install if install else []), *manifest.checks,
                *(command for service in manifest.services
                  for command in (service.init, service.start, service.health, service.stop) if command is not None)]
    results = dict(progress)
    for command in declared:
        results.setdefault(command.id, CheckResult(id=command.id, required=command.required, status='not_run',
            output='private runtime run ended before this result', manifest_digest=digest, context=scope))
    _bound_outputs(results)
    order = {command.id: index for index, command in enumerate(declared)}
    return VerificationReport(execution_source_digest=execution_source_digest, manifest_digest=digest, scope=scope,
                              results=sorted(results.values(), key=lambda result: order.get(result.id, -1)))


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
        await _scan_bounds(runtime_lease)
        kwargs["scratch"] = str(runtime_lease.scratch)
        report = await run_guarded(runtime_lease, commands(manifest, **kwargs, runtime_env=runtime_lease.activation_env))
        await _scan_bounds(runtime_lease)
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
        incomplete: list[str] = []
        cause: BaseException | None = None
        progress: dict[str, CheckResult] = {}
        try:
            try:
                report = await run_guarded(lease, commands(manifest, **kwargs, progress=progress,
                                                            runtime_env=lease.activation_env))
                await _scan_bounds(lease)
            except TimeoutError as exc:
                if _cancelling():
                    raise asyncio.CancelledError from exc
                incomplete.append('deadline_exhausted')
                cause = exc
            except (RuntimeError, OSError, ValueError, RuntimeScanStoppedError) as exc:
                # A guest exit, bounds violation or failed scan still holds; keep what the commands did. A pending
                # cancellation always wins: it never becomes a hold.
                if _cancelling():
                    raise asyncio.CancelledError from exc
                incomplete.append('runtime_failed')
                cause = exc
            if incomplete:
                if report is None:
                    report = _partial_report(manifest, progress, install=install, scope=scope,
                                             execution_source_digest=execution_source_digest)
            else:
                if install and report.passed:
                    lease.installation_passed = True
                    try:
                        receipt = await seal_runtime(lease, manifest_digest=manifest.digest(),
                                                     source_digest=execution_source_digest, deadline=deadline)
                    except (RuntimeError, ValueError, OSError, TimeoutError) as exc:
                        if _cancelling():
                            raise asyncio.CancelledError from exc
                        lease.installation_passed = False
                        incomplete.append('seal_failed')
                        cause = exc
        finally:
            try:
                cleanup = await lease.close(deadline=deadline)
            except (TimeoutError, RuntimeError, OSError) as exc:
                # A failed close is an unconfirmed teardown; it never discards the command evidence, and a pending
                # cancellation still wins.
                if _cancelling():
                    raise asyncio.CancelledError from exc
                cleanup = None
                cause = cause or exc
        teardown = [] if cleanup is not None and cleanup.confirmed and cleanup.clean_shutdown and \
            cleanup.bounds_respected else ([name for name in TEARDOWN_CONDITIONS
                                            if getattr(cleanup, name, True) is False] or ['unconfirmed'])
        if incomplete or teardown:
            report.runtime_digest = spec.digest()
            report.runtime_phase = 'installer' if install else 'consumer'
            report.runtime_cleanup = not teardown
            report.runtime_artifacts = None
            report.runtime_incomplete = [*incomplete, *teardown]
            if cause is not None:
                report.runtime_incomplete_detail = _cause_detail(cause, secret_names)
            raise PrivateRuntimeIncompleteError(report.runtime_incomplete, report)
        report.runtime_digest = spec.digest()
        report.runtime_phase = 'installer' if install else 'consumer'
        report.runtime_artifacts = receipt
        report.runtime_cleanup = True
        return report

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


DETAIL_MAX_CHARS = 16000


def _cause_detail(cause: BaseException, secret_names: Sequence[str], extra: frozenset[str] = frozenset()) -> str:
    """Exception class and message with known secret values removed, never clipped: only the owner can
    pattern-redact, and any cut made before that could split a credential into an unrecognisable fragment. A
    message beyond the bound is replaced by the exception class alone."""
    import os

    from .runner import _redact

    secrets = frozenset(os.environ[name] for name in secret_names if os.environ.get(name))
    text = _redact(f'{type(cause).__name__}: {cause}', {}, extra_values=secrets | extra)
    return text if len(text) <= DETAIL_MAX_CHARS else type(cause).__name__


# What the guest's Docker still holds once every command and service stop has run, by name so an author can tell
# which record left it. The seal check refuses to shut down while any of it remains.
RESIDUAL_LIST = ("printf 'containers=%s\\n' \"$(docker ps -a --format '{{.Names}}' | tr '\\n' ' ')\"; "
                 "printf 'volumes=%s\\n' \"$(docker volume ls -q | tr '\\n' ' ')\"; "
                 "printf 'networks=%s\\n' \"$(docker network ls --filter type=custom --format '{{.Name}}' | tr '\\n' ' ')\"")
RESIDUAL_REMOVE = ("docker ps -aq | xargs -r docker rm -f -v >/dev/null; docker volume ls -q | xargs -r docker volume rm -f "
                   ">/dev/null; docker network ls --filter type=custom -q | xargs -r docker network rm >/dev/null")
RESIDUAL_GUIDANCE = ('Every service stop must remove the Docker state its service created, including anonymous volumes '
                     '(docker rm -f -v) and custom networks: the guest cannot shut down cleanly while any remain.')
MAX_RESIDUAL_NAMES = 16


async def _reclaim_residual_state(lease: RuntimeLease, manifest: EnvironmentManifest, report: VerificationReport, *,
                                  deadline: float, secret_names: Sequence[str], redact_values: frozenset[str]) -> None:
    """Docker state left after the last command fails the records that should have removed it, then is removed, so the
    guest still shuts down cleanly and the run stays repairable.

    The seal check would otherwise refuse the shutdown and hold the run as incomplete, with no cause and no repair
    (Dettmore e2e-04: a postgres container's anonymous volume). Blamed are the service stops that ran, or failing
    those the commands that ran Docker. State no record could have left is not removed: the seal check still refuses it.
    Resource names are guest output, so they are redacted like command output before they reach a result.
    """
    import os

    from .runner import _redact, _secret_values

    try:
        _code, output = await lease.ssh(RESIDUAL_LIST, deadline=deadline)
    except (RuntimeError, OSError, TimeoutError):
        return
    left = {kind: names.split() for kind, _, names in
            (line.partition('=') for line in output.decode(errors='replace').splitlines()) if names.split()}
    if not left:
        return
    ran = {result.id: result for result in report.results if result.status != 'not_run'}
    blamed = [ran[service.stop.id] for service in manifest.services if service.stop.id in ran] or [
        ran[command.id] for command in [*manifest.install, *manifest.checks]
        if command.id in ran and any('docker' in word for word in command.argv)]
    if not blamed:
        return
    found = '; '.join(f'{kind} {" ".join(names[:MAX_RESIDUAL_NAMES])}' for kind, names in left.items())
    known = frozenset(os.environ[name] for name in secret_names if os.environ.get(name)) | redact_values | frozenset(
        _secret_values(dict(lease.activation_env)))
    detail = _redact(f'teardown found Docker state left behind: {found}. {RESIDUAL_GUIDANCE}', {}, extra_values=known)
    for result in blamed:
        result.status = 'fail'
        result.output = f'{result.output}\n{detail}' if result.output else detail
    with contextlib.suppress(RuntimeError, OSError, TimeoutError):
        await lease.ssh(RESIDUAL_REMOVE, deadline=deadline, check=False)


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
    redact_values: frozenset[str] = frozenset(),
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
        # The live lease is sealed and stopped by its owner; leftover state must fail here, not at that seal.
        await _reclaim_residual_state(runtime_lease, manifest, report, deadline=runtime_lease.deadline,
                                      secret_names=secret_names, redact_values=redact_values)
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
                await _reclaim_residual_state(lease, manifest, report, deadline=deadline, secret_names=secret_names,
                                              redact_values=redact_values)
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
            if cleanup is not None and not cleanup.clean_shutdown and cause is None and getattr(lease, 'shutdown_error', None):
                # The guest refused or failed its shutdown; close() suppresses that, so without this the hold has no cause.
                cause = RuntimeError(f'private runtime guest did not shut down cleanly: {lease.shutdown_error}')
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
                report.runtime_incomplete_detail = _cause_detail(cause, secret_names, redact_values)
            raise PrivateRuntimeIncompleteError(report.runtime_incomplete, report)
        report.runtime_digest = spec.digest()
        report.runtime_phase = 'installer' if install else 'consumer'
        report.runtime_artifacts = receipt
        report.runtime_cleanup = True
        return report

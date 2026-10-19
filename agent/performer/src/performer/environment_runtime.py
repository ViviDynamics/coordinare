"""Per-job adapter for an explicitly reviewed optional service runtime."""
from __future__ import annotations

import os
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt
from coordinare_service_inference.environment.runtime_supervisor import open_runtime

if TYPE_CHECKING:
    from coordinare_service_inference.environment.models import EnvironmentManifest
    from coordinare_service_inference.environment.runtime_supervisor import RuntimeLease

    from performer.models import Score, Stand


def _refuse(message: str) -> NoReturn:
    raise ValueError(message) from None


async def open_score_runtime(score: Score, stand: Stand, manifest: EnvironmentManifest, scratch: Path) -> RuntimeLease:
    spec = manifest.runtime
    if spec is None:
        _refuse('private runtime contract is missing')
    try:
        source = ExecutionSourceIdentity.model_validate(score.environment_execution_source)
    except ValueError:
        _refuse('private runtime requires complete source identity')
    receipt = None
    install = score.role == 'env_bootstrap'
    deadline = time.monotonic()+spec.policy.resources.max_seconds
    if install:
        if score.environment_attempt_expires_at is None:
            _refuse('private runtime bootstrap requires original attempt expiry')
        deadline = min(deadline, time.monotonic()+score.environment_attempt_expires_at-time.time())
    else:
        try:
            receipt = RuntimeArtifactReceipt.model_validate(score.environment_runtime_artifacts)
        except ValueError:
            _refuse('private runtime consumer requires bound artifact receipt')
        receipt.require_binding(manifest_digest=manifest.digest(), source_digest=source.digest(), runtime_digest=spec.digest())
    return await open_runtime(spec, artifacts=receipt, repo=stand.path, cache=Path(score.env_cache_path),
        scratch=scratch, install=install, deadline=deadline, manifest_digest=manifest.digest(), source_digest=source.digest(),
        purpose='installer' if install else 'live_consumer')


async def close_score_runtime(lease: RuntimeLease) -> None:
    report = await lease.close(deadline=lease.deadline)
    if not report.bounds_respected:
        _refuse('private runtime resource or deadline limit failed')
    if not report.confirmed or not report.clean_shutdown:
        _refuse('private runtime teardown or immutable artifact verification failed')


def runtime_activation(lease: RuntimeLease) -> str:
    lines = []
    for key, value in lease.activation_env.items():
        if key != 'PATH':
            lines.append('export ' + key + '=' + shlex.quote(value))
    prefix = lease.activation_env.get('PATH', '').split(os.pathsep)[0]
    lines.append('export PATH=' + shlex.quote(prefix) + ':"$PATH"')
    return '\n'.join(lines) + '\n'

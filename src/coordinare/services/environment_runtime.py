"""Daemon-owned receipt transfer for the optional private-service runtime."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.models import (
    CoverageAssessment,
    EnvironmentManifest,
    validate_current_coverage,
)
from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt, RuntimePolicy

from coordinare.services.environment_acceptance import (
    accepted_environment,
    execution_report,
    saved_execution_accepted,
)
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_repair import journal_for, remaining_seconds
from coordinare.services.environment_runtime_evidence import require_runtime_target

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.models.env_cache import EnvCacheState


def _refuse(message: str) -> NoReturn:
    raise ValueError(message) from None


@dataclass(frozen=True)
class RuntimeVerifierContext:
    environment: dict[str, str]
    expires_at: float


@dataclass(frozen=True)
class RuntimeVerifierSource:
    root: Path
    context: RuntimeVerifierContext


@dataclass(frozen=True)
class RuntimeVerifierInput:
    data: str | bytes
    timeout_seconds: float


def unpack_verifier_source(source: Path | RuntimeVerifierSource) -> tuple[Path, RuntimeVerifierContext | None]:
    if isinstance(source, RuntimeVerifierSource):
        return source.root, source.context
    return source, None


def unpack_verifier_input(value: str | bytes | RuntimeVerifierInput, default_timeout: float) -> tuple[str | bytes, float]:
    if isinstance(value, RuntimeVerifierInput):
        return value.data, value.timeout_seconds
    return value, default_timeout


def verifier_deadline(cache: EnvCacheState, context: RuntimeVerifierContext | None,
                      default_timeout: float) -> tuple[RuntimeVerifierContext | None, float]:
    manifest = accepted_environment(cache)
    if manifest is not None and manifest.runtime is not None:
        context = context or runtime_verifier_context(cache)
        if context is None or context.expires_at <= time.time():
            _refuse('private runtime clean verification deadline exhausted')
        return context, time.monotonic()+context.expires_at-time.time()
    if context is not None:
        _refuse('native manifest cannot use a private runtime verifier context')
    return None, time.monotonic()+default_timeout


def launch_runtime_policy(metadata: dict[str, Any]) -> RuntimePolicy | None:
    """Launch limits come only from the exact independently reviewed contract."""
    raw = metadata.get('environment_manifest')
    if not isinstance(raw, dict) or raw.get('runtime') is None:
        return None
    manifest = EnvironmentManifest.model_validate(raw)
    review = CoverageAssessment.model_validate(metadata.get('environment_assessment'))
    validate_current_coverage(manifest, review)
    if metadata.get('environment_manifest_digest') != manifest.digest() or manifest.runtime is None:
        _refuse('private runtime launch requires the exact reviewed manifest digest')
    return manifest.runtime.policy


def runtime_verifier_options(manifest: EnvironmentManifest, source_root: Path) -> list[str]:
    runtime = manifest.runtime
    if runtime is None:
        _refuse('private runtime verifier has no accepted policy')
    scratch = source_root/'runtime-scratch'
    scratch.mkdir(mode=0o700)
    limits = runtime.policy.resources
    return ['--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--cpus', str(limits.cpus),
            '--memory', str(limits.memory_mib)+'m', '--memory-swap', str(limits.memory_mib)+'m',
            '--pids-limit', '512', '-v', str(scratch)+':/tmp']


def runtime_verifier_context(cache: EnvCacheState, *, now: float | None = None) -> RuntimeVerifierContext | None:
    """A later health job has its own bounded lifetime only after prior clean acceptance."""
    if (cache.environment_manifest or {}).get('runtime') is None:
        if cache.environment_runtime_artifacts is not None:
            _refuse('native manifest cannot use private runtime artifacts')
        return None
    from coordinare.services.environment_acceptance import (
        accepted_environment,
    )

    now = time.time() if now is None else now
    try:
        manifest = accepted_environment(cache)
        if manifest is None or manifest.runtime is None:
            _refuse('private runtime requires independently accepted contract')
        require_runtime_target(manifest, cache)
        source = ExecutionSourceIdentity.model_validate(cache.environment_execution_source)
        receipt = RuntimeArtifactReceipt.model_validate(cache.environment_runtime_artifacts)
        if source.source_commit != manifest.source_commit:
            _refuse('private runtime source identity differs')
        receipt.require_binding(manifest_digest=manifest.digest(), source_digest=source.digest(),
                                runtime_digest=manifest.runtime.digest())
        journal = journal_for(cache)
        expires_at = now+manifest.runtime.policy.resources.max_seconds
        if journal.status == 'ready':
            if not saved_execution_accepted(cache):
                _refuse('private runtime later consumer requires prior clean execution')
        else:
            if journal.status != 'executing':
                _refuse('private runtime attempt is not executing')
            remaining_seconds(cache, now=now)
            expires_at = min(expires_at, journal.expires_at)
        encoded = receipt.model_dump_json()
        if len(encoded.encode()) > 64*1024:
            _refuse('private runtime artifact receipt exceeds transfer bound')
    except (ValueError, EnvironmentDiscoveryError):
        _refuse('private runtime verifier context is missing, expired or differs')
    return RuntimeVerifierContext({'COORDINARE_ENV_RUNTIME_EXPIRES_AT': str(expires_at),
                                   'COORDINARE_ENV_RUNTIME_ARTIFACTS': encoded}, expires_at)



def admit_installer_runtime(cache: EnvCacheState, evidence: dict[str, Any] | None, *, cleanup_confirmed: bool) -> None:
    """Record only sealed parent-collected output after the writer has been reaped."""
    if (cache.environment_manifest or {}).get('runtime') is None:
        return
    if not cleanup_confirmed or evidence is None:
        _refuse('private runtime installer requires confirmed outer cleanup and execution evidence')

    report = execution_report(json.dumps(evidence), cache, publish=False, runtime_phase='installer')
    if not report.passed:
        _refuse('private runtime installer checks or platform did not pass')
    if report.runtime_artifacts is None:
        _refuse('private runtime installer sealed receipt is missing')
    if (cache.environment_runtime_artifacts is not None
            and cache.environment_runtime_artifacts != report.runtime_artifacts.model_dump(mode='json')):
        _refuse('private runtime installer artifact generation changed without invalidation')
    cache.environment_runtime_artifacts = report.runtime_artifacts.model_dump(mode='json')

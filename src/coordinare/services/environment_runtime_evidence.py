"""Validate optional runtime evidence without importing daemon orchestration."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NoReturn

from coordinare_service_inference.environment.models import ExecutionCapabilities
from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt

if TYPE_CHECKING:
    from coordinare_service_inference.environment.models import (
        EnvironmentManifest,
        VerificationReport,
    )

    from coordinare.models.env_cache import EnvCacheState

def _refuse(message: str) -> NoReturn:
    raise ValueError(message) from None

def require_runtime_target(manifest: EnvironmentManifest, cache: EnvCacheState) -> None:
    try:
        target = ExecutionCapabilities.model_validate(cache.environment_capabilities)
    except ValueError:
        _refuse('private runtime capabilities are missing or invalid')
    if (manifest.runtime is None or target.runtime != manifest.runtime.policy
            or target.digest() != manifest.capabilities_digest):
        _refuse('private runtime policy differs from accepted capabilities')

def validate_runtime_evidence(report: VerificationReport, manifest: EnvironmentManifest,
                              cache: EnvCacheState, *, phase: Literal['installer', 'consumer']) -> None:
    runtime = manifest.runtime
    if runtime is None:
        if any(value is not None for value in (report.runtime_digest, report.runtime_phase,
                                              report.runtime_artifacts, report.runtime_cleanup)):
            _refuse('native manifest cannot use private runtime evidence')
        return
    require_runtime_target(manifest, cache)
    if (report.runtime_digest != runtime.digest() or report.runtime_phase != phase
            or report.platform != runtime.policy.identity.carrier_platform
            or report.execution_source_digest is None
            or report.runtime_cleanup is not True or report.runtime_artifacts is None):
        _refuse('private runtime execution or cleanup evidence is missing or differs')
    try:
        report.runtime_artifacts.require_binding(manifest_digest=manifest.digest(),
            source_digest=report.execution_source_digest, runtime_digest=runtime.digest())
        if phase == 'consumer':
            saved = RuntimeArtifactReceipt.model_validate(cache.environment_runtime_artifacts)
            if saved != report.runtime_artifacts:
                _refuse('private runtime consumer artifact receipt differs from installer')
    except ValueError:
        _refuse('private runtime artifact receipt is missing or differs')


def validate_failed_installer_runtime(report: VerificationReport, manifest: EnvironmentManifest,
                                      cache: EnvCacheState) -> None:
    """A failed installer has cleanup evidence, never a sealed successful artifact."""
    require_runtime_target(manifest, cache)
    runtime = manifest.runtime
    if (runtime is None or report.runtime_digest != runtime.digest()
            or report.runtime_phase != 'installer' or report.runtime_cleanup is not True
            or report.platform != runtime.policy.identity.carrier_platform
            or report.runtime_artifacts is not None):
        _refuse('failed installer runtime identity or cleanup is missing or differs')

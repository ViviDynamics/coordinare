"""Trusted dispatch contracts; installer output is never a verification report."""
from __future__ import annotations

from contextlib import AsyncExitStack
from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt
from coordinare_service_inference.environment.runtime_supervisor import seal_runtime

from performer.environment_runtime import close_score_runtime, open_score_runtime, runtime_activation

from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.execution_source_checkout import checkout_identity

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from coordinare_service_inference.environment.models import EnvironmentManifest

    from performer.models import Score, Stand


class EnvironmentReadinessError(ValueError):
    """The accepted cache failed consumer checks and must be rebuilt."""


def _refuse_contract(reason: str) -> NoReturn:
    """Reject a dispatch contract without exposing schema input values."""
    raise ValueError(reason) from None


def _require_ready(result: dict[str, Any]) -> None:
    if not result["passed"]:
        reason = "reviewed environment checks failed: " + result["reason"]
        raise EnvironmentReadinessError(reason)


def _require_services_ready(results: Any) -> None:
    if any(item.required and item.status != "pass" for item in results):
        reason = "reviewed environment required service failed"
        raise EnvironmentReadinessError(reason)


def accepted_manifest(metadata: Any) -> EnvironmentManifest:
    """Validate the exact independently accepted contract carried by the daemon."""
    def get(name: str) -> Any:
        return metadata.get(name) if isinstance(metadata, dict) else getattr(metadata, name, None)

    raw, assessment = get("environment_manifest"), get("environment_assessment")
    if not isinstance(raw, dict) or not isinstance(assessment, dict):
        _refuse_contract("reviewed environment manifest and assessment are required")
    if any(len(json.dumps(item, ensure_ascii=False).encode()) > 256 * 1024 for item in (raw, assessment)):
        _refuse_contract("environment contract exceeds size limit")
    from coordinare_service_inference.environment.models import CoverageAssessment, EnvironmentManifest, validate_current_coverage

    try:
        manifest = EnvironmentManifest.model_validate(raw)
        review = CoverageAssessment.model_validate(assessment)
    except ValueError:
        _refuse_contract("environment manifest or review schema is malformed or unsupported")
    if not manifest.inventory_digest or review.inventory_digest != manifest.inventory_digest:
        _refuse_contract("environment inventory identity does not match accepted manifest")
    if not manifest.capabilities_digest or review.capabilities_digest != manifest.capabilities_digest:
        _refuse_contract("environment capability identity does not match accepted manifest")
    digest = manifest.digest()
    if get("environment_manifest_digest") != digest or review.manifest_digest != digest:
        _refuse_contract("environment review digest does not match manifest")
    if review.source_commit != manifest.source_commit or review.verdict != "accept":
        _refuse_contract("environment review does not accept this source commit")
    if review.symphony_name and review.symphony_name != manifest.symphony_name:
        _refuse_contract("environment review symphony does not match manifest")
    if not review.inputs or any(review.inputs.get(path) != digest for path, digest in manifest.inputs.items()):
        _refuse_contract("environment review source identities do not match manifest")
    if not set(review.sources).issubset(review.inputs):
        _refuse_contract("environment review cites uninspected sources")
    if not {item.source for item in manifest.requirements}.issubset(review.inputs):
        _refuse_contract("environment review lacks requirement sources")
    try:
        validate_current_coverage(manifest, review)
    except ValueError:
        _refuse_contract('current independent coverage findings require refresh')
    return manifest


async def execute_contract(score: Score, stand: Stand, *, install: bool = False) -> dict[str, Any]:
    """Run accepted commands in an isolated scratch directory and return parent-collected outcomes."""
    from coordinare_service_inference.environment.runner import run_manifest

    manifest = accepted_manifest(score)

    try:
        expected_source = ExecutionSourceIdentity.model_validate(score.environment_execution_source)
    except ValueError:
        _refuse_contract('complete execution source identity missing; rebuild the cache')
    if expected_source.source_commit != manifest.source_commit:
        _refuse_contract('complete execution source commit differs')
    if install:
        actual_source = await checkout_identity(stand.path, commit=manifest.source_commit, review_inputs=manifest.inputs)
        if actual_source != expected_source:
            _refuse_contract('installer checkout differs from accepted complete source')
    with tempfile.TemporaryDirectory(prefix="coordinare-environment-") as scratch:
        runtime_kwargs = {}
        lease = getattr(stand, "environment_runtime", None)
        if manifest.runtime is not None:

            runtime_kwargs["runtime_lease"] = lease
            if lease is None and not install:
                runtime_kwargs["runtime_artifacts"] = RuntimeArtifactReceipt.model_validate(score.environment_runtime_artifacts)
            if lease is not None and lease.install and not install and not lease.installation_passed:
                _refuse_contract("required installer actions have not passed")
        report = await run_manifest(
            manifest, cache=Path(score.env_cache_path), repo=stand.path,
            scratch=Path(scratch), install=install, scope="" if score.role == "env_bootstrap" else score.role,
            secret_names=score.environment_secret_names,
            execution_source_digest=expected_source.digest(), **runtime_kwargs,
        )
    if manifest.runtime is not None and lease is not None and lease.install and not install and report.passed:

        receipt = await seal_runtime(lease, manifest_digest=manifest.digest(), source_digest=expected_source.digest(), deadline=lease.deadline)
        report.runtime_artifacts = receipt
        score.environment_runtime_artifacts = receipt.model_dump(mode="json")
        cleanup = await lease.close(deadline=lease.deadline)
        report.runtime_cleanup = cleanup.confirmed and cleanup.clean_shutdown and cleanup.bounds_respected
        if not report.runtime_cleanup:
            _refuse_contract("private runtime teardown did not pass")
        stand.environment_runtime = None
    failures = [f"{item.id}: {item.status}" for item in report.results if item.status != "pass"]
    return {
        "passed": report.passed,
        "reason": "; ".join(failures)[-1000:],
        "verify_basis": "daemon_rendered" if report.passed else None,
        "environment_verification": report.model_dump(),
    }


async def prepare_environment(score: Score, stand: Stand) -> None:
    """Activate declarative metadata and hold only explicitly declared services for this job."""

    from coordinare_service_inference.environment.runner import manifest_environment, manifest_services

    manifest = accepted_manifest(score)
    lifecycle = AsyncExitStack()
    stand.environment_lifecycle = lifecycle
    try:
        scratch = lifecycle.enter_context(tempfile.TemporaryDirectory(prefix="coordinare-consumer-"))
        roots = {"cache": Path(score.env_cache_path), "repo": stand.path, "scratch": Path(scratch)}
        lease = None
        if manifest.runtime is not None:

            lease = await open_score_runtime(score, stand, manifest, Path(scratch))
            stand.environment_runtime = lease
            lifecycle.push_async_callback(close_score_runtime, lease)
        runtime_kwargs = {"runtime_env": lease.activation_env} if lease is not None else {}
        stand.cache_env = manifest_environment(manifest, **roots, secret_names=score.environment_secret_names, **runtime_kwargs)
        # Bootstrap actions create missing paths; verification runs after installation.
        if score.role != "env_bootstrap":
            result = await execute_contract(score, stand)
            _require_ready(result)
            from coordinare_service_inference.environment.render import render_activate

            activation = Path(scratch) / "activate.sh"
            text = render_activate(manifest, cache_mount_path=score.env_cache_path)
            if lease is not None:

                text += runtime_activation(lease)
            activation.write_text(text)
            activation.chmod(0o400)
            stand.cache_env.update({"BASH_ENV": str(activation), "COORDINARE_REPO_DIR": str(stand.path),
                                    "COORDINARE_SCRATCH_DIR": scratch})
            results = await lifecycle.enter_async_context(
                manifest_services(manifest, **roots, scope=score.role, secret_names=score.environment_secret_names, **runtime_kwargs),
            )
            _require_services_ready(results)
            from performer.workspace import _mark_services_healthy

            _mark_services_healthy()
    except BaseException:
        await close_environment(stand)
        raise


async def close_environment(stand: Stand) -> None:
    """Teardown declared services even after errors or cancellation."""
    lifecycle, stand.environment_lifecycle = getattr(stand, "environment_lifecycle", None), None
    try:
        if lifecycle is not None:
            await lifecycle.aclose()
    finally:
        stand.environment_runtime = None

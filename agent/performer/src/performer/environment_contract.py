"""Trusted dispatch contracts; installer output is never a verification report."""
from __future__ import annotations

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
    from coordinare_service_inference.environment.models import CoverageAssessment, EnvironmentManifest

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
    return manifest


async def execute_contract(score: Score, stand: Stand, *, install: bool = False) -> dict[str, Any]:
    """Run accepted commands in an isolated scratch directory and return parent-collected outcomes."""
    from coordinare_service_inference.environment.runner import run_manifest

    manifest = accepted_manifest(score)
    from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
    from coordinare_service_inference.environment.execution_source_checkout import checkout_identity

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
        report = await run_manifest(
            manifest, cache=Path(score.env_cache_path), repo=stand.path,
            scratch=Path(scratch), install=install, scope="" if score.role == "env_bootstrap" else score.role,
            secret_names=score.environment_secret_names,
            execution_source_digest=expected_source.digest(),
        )
    failures = [f"{item.id}: {item.status}" for item in report.results if item.status != "pass"]
    return {
        "passed": report.passed,
        "reason": "; ".join(failures)[-1000:],
        "verify_basis": "daemon_rendered" if report.passed else None,
        "environment_verification": report.model_dump(),
    }


async def prepare_environment(score: Score, stand: Stand) -> None:
    """Activate declarative metadata and hold only explicitly declared services for this job."""
    from contextlib import AsyncExitStack

    from coordinare_service_inference.environment.runner import manifest_environment, manifest_services

    manifest = accepted_manifest(score)
    lifecycle = AsyncExitStack()
    stand.environment_lifecycle = lifecycle
    try:
        scratch = lifecycle.enter_context(tempfile.TemporaryDirectory(prefix="coordinare-consumer-"))
        roots = {"cache": Path(score.env_cache_path), "repo": stand.path, "scratch": Path(scratch)}
        stand.cache_env = manifest_environment(manifest, **roots, secret_names=score.environment_secret_names)
        # Bootstrap actions create missing paths; verification runs after installation.
        if score.role != "env_bootstrap":
            result = await execute_contract(score, stand)
            _require_ready(result)
            from coordinare_service_inference.environment.render import render_activate

            activation = Path(scratch) / "activate.sh"
            activation.write_text(render_activate(manifest, cache_mount_path=score.env_cache_path))
            activation.chmod(0o400)
            stand.cache_env.update({"BASH_ENV": str(activation), "COORDINARE_REPO_DIR": str(stand.path),
                                    "COORDINARE_SCRATCH_DIR": scratch})
            results = await lifecycle.enter_async_context(
                manifest_services(manifest, **roots, scope=score.role, secret_names=score.environment_secret_names),
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
    if lifecycle is not None:
        await lifecycle.aclose()

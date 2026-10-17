"""Trusted dispatch contracts; installer output is never a verification report."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coordinare_service_inference.environment.models import EnvironmentManifest

    from performer.models import Score, Stand


class EnvironmentReadinessError(ValueError):
    """The accepted cache failed consumer checks and must be rebuilt."""


def accepted_manifest(metadata: Any) -> EnvironmentManifest:
    """Validate the exact independently accepted contract carried by the daemon."""
    def get(name: str) -> Any:
        return metadata.get(name) if isinstance(metadata, dict) else getattr(metadata, name, None)

    raw, assessment = get("environment_manifest"), get("environment_assessment")
    if not isinstance(raw, dict) or not isinstance(assessment, dict):
        raise ValueError("reviewed environment manifest and assessment are required")
    if any(len(json.dumps(item, ensure_ascii=False).encode()) > 256 * 1024 for item in (raw, assessment)):
        raise ValueError("environment contract exceeds size limit")
    from coordinare_service_inference.environment.models import CoverageAssessment, EnvironmentManifest

    try:
        manifest = EnvironmentManifest.model_validate(raw)
        review = CoverageAssessment.model_validate(assessment)
    except ValueError:
        raise ValueError("environment manifest or review schema is malformed or unsupported") from None
    digest = manifest.digest()
    if get("environment_manifest_digest") != digest or review.manifest_digest != digest:
        raise ValueError("environment review digest does not match manifest")
    if review.source_commit != manifest.source_commit or review.verdict != "accept":
        raise ValueError("environment review does not accept this source commit")
    if review.symphony_name and review.symphony_name != manifest.symphony_name:
        raise ValueError("environment review symphony does not match manifest")
    if not review.inputs or any(review.inputs.get(path) != digest for path, digest in manifest.inputs.items()):
        raise ValueError("environment review source identities do not match manifest")
    if not set(review.sources).issubset(review.inputs):
        raise ValueError("environment review cites uninspected sources")
    if not {item.source for item in manifest.requirements}.issubset(review.inputs):
        raise ValueError("environment review lacks requirement sources")
    return manifest


async def execute_contract(score: Score, stand: Stand, *, install: bool = False) -> dict[str, Any]:
    """Run accepted commands in an isolated scratch directory and return parent-collected outcomes."""
    from coordinare_service_inference.environment.runner import run_manifest

    manifest = accepted_manifest(score)
    with tempfile.TemporaryDirectory(prefix="coordinare-environment-") as scratch:
        report = await run_manifest(
            manifest, cache=Path(score.env_cache_path), repo=stand.path,
            scratch=Path(scratch), install=install, scope="" if score.role == "env_bootstrap" else score.role,
            secret_names=score.environment_secret_names,
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
            if not result["passed"]:
                raise EnvironmentReadinessError("reviewed environment checks failed: " + result["reason"])
            from coordinare_service_inference.environment.render import render_activate

            activation = Path(scratch) / "activate.sh"
            activation.write_text(render_activate(manifest, cache_mount_path=score.env_cache_path))
            activation.chmod(0o400)
            stand.cache_env.update({"BASH_ENV": str(activation), "COORDINARE_REPO_DIR": str(stand.path),
                                    "COORDINARE_SCRATCH_DIR": scratch})
            results = await lifecycle.enter_async_context(
                manifest_services(manifest, **roots, scope=score.role, secret_names=score.environment_secret_names),
            )
            if any(item.required and item.status != "pass" for item in results):
                raise EnvironmentReadinessError("reviewed environment required service failed")
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

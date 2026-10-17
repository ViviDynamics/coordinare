"""Validate the daemon-owned review before any environment readiness decision."""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.models import (
    CoverageAssessment,
    EnvironmentManifest,
    VerificationReport,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from coordinare.models.env_cache import EnvCacheState


def accepted_environment(cache: EnvCacheState) -> EnvironmentManifest | None:
    """Return only the complete, matching independently accepted contract."""
    try:
        manifest = EnvironmentManifest.model_validate(cache.environment_manifest)
        assessment = CoverageAssessment.model_validate(cache.environment_assessment)
    except (ValidationError, TypeError):
        return None
    if (
        assessment.verdict != "accept"
        or assessment.manifest_digest != manifest.digest()
        or assessment.source_commit != manifest.source_commit
        or cache.bootstrap_commit_sha != manifest.source_commit
        or manifest.symphony_name != cache.symphony_name
        or (assessment.symphony_name and assessment.symphony_name != manifest.symphony_name)
        or any(assessment.inputs.get(path) != digest for path, digest in manifest.inputs.items())
        or not assessment.sources
        or any(path not in assessment.inputs for path in assessment.sources)
    ):
        return None
    return manifest


def immutable_verifier_image(image: str) -> str:
    """Require content identity so an image tag cannot silently invalidate evidence."""
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("environment verification requires an immutable image@sha256 digest")
    return image


def consumer_image_matches(evidence: dict[str, Any] | None, image: str) -> bool:
    """A different consumer image needs its own clean execution evidence."""
    try:
        immutable_verifier_image(image)
    except (ValueError, TypeError):
        return False
    return evidence is not None and evidence.get("image_ref") == image


def execution_report(text: str, cache: EnvCacheState, *, publish: bool = True) -> VerificationReport:
    """Validate parent-collected results; success text cannot substitute for execution."""
    manifest = accepted_environment(cache)
    if manifest is None:
        raise ValueError("required accepted environment contract is missing")
    if len(text.encode()) > 1024 * 1024:
        raise ValueError("required execution evidence exceeds its bound")
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or not data.get("platform"):
            raise ValueError("execution evidence must be an object")
        data.pop("passed", None)  # Recompute, never trust an asserted aggregate.
        report = VerificationReport.model_validate(data)
    except (ValueError, TypeError) as exc:
        raise ValueError("required execution evidence is malformed or missing") from exc
    if report.scope != "":
        raise ValueError("required clean execution evidence must cover every scope")
    if report.manifest_digest != manifest.digest():
        raise ValueError("required execution evidence has the wrong manifest identity")
    by_id = {result.id: result for result in report.results}
    if len(by_id) != len(report.results):
        raise ValueError("required execution evidence contains duplicate checks")
    required = [check for check in manifest.checks if check.required
                and (not report.scope or not check.scope or report.scope in check.scope)]
    for service in manifest.services:
        if service.required:
            required.extend([service.start, service.health, service.stop])
            if service.init is not None:
                required.append(service.init)
    if not required or any(
        check.id not in by_id or not by_id[check.id].required
        or by_id[check.id].manifest_digest != manifest.digest()
        for check in required
    ):
        raise ValueError("required execution evidence omitted a check or its identity")
    if any(
        result.status == "pass" and result.exit_code != 0
        for check in manifest.checks if check.required for result in [by_id[check.id]]
    ):
        raise ValueError("required passing checks must record exit code zero")
    if publish:
        cache.environment_verification = report.model_dump(mode="json")
    return report


def saved_execution_accepted(cache: EnvCacheState) -> bool:
    """Persisted flags cannot claim readiness without complete bound execution evidence."""
    evidence = cache.environment_verification
    if not isinstance(evidence, dict):
        return False
    data = dict(evidence)
    try:
        immutable_verifier_image(data.pop("image_ref", ""))
        return bool(execution_report(json.dumps(data), cache, publish=False).passed)
    except (ValueError, TypeError):
        return False

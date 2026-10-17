"""Real reviewed contracts used by environment transport and security fixtures."""
from __future__ import annotations

import hashlib

from coordinare_service_inference.environment.models import EnvironmentManifest


def reviewed_metadata(*, checks=None, install=None, services=None, activation=None, source_commit="a" * 40,
                      symphony_name="sym", inputs=None, empty_reason="Image baseline is sufficient"):
    manifest = EnvironmentManifest.model_validate({
        "schema_version": 1, "symphony_name": symphony_name, "source_commit": source_commit,
        "inputs": inputs if inputs is not None else {"tools.plan": hashlib.sha256(b"baseline").hexdigest()},
        "requirements": [], "install": install or [],
        "checks": checks or [{"id": "baseline", "argv": ["true"]}],
        "activation": activation or {}, "services": services or [],
        "empty_reason": empty_reason, "policy_version": "351-v1",
    })
    return {"environment_manifest": manifest.model_dump(), "environment_manifest_digest": manifest.digest(),
            "environment_assessment": {"schema_version": 1, "manifest_digest": manifest.digest(),
                "source_commit": source_commit, "verdict": "accept", "rationale": "Read source requirements",
                "sources": list(manifest.inputs), "inputs": dict(manifest.inputs), "policy_version": "351-v1"}}


def reviewed_cache(cache, *, files=None, source_commit="a" * 40):
    """Bind a persisted cache fixture to actual inspected source bytes."""
    contents = files if files is not None else {"tools.plan": "baseline"}
    inputs = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in contents.items()}
    metadata = reviewed_metadata(inputs=inputs, source_commit=source_commit, symphony_name=cache.symphony_name)
    cache.environment_manifest = metadata["environment_manifest"]
    cache.environment_assessment = metadata["environment_assessment"]
    cache.bootstrap_commit_sha = source_commit
    import json

    cache.environment_verification = json.loads(verification_output(cache.environment_manifest))
    cache.environment_verification["image_ref"] = "trusted/global@sha256:" + "a" * 64
    return cache


def verification_output(manifest=None):
    """Fake container's parent-collected clean-run JSON, bound to its accepted contract."""
    from coordinare_service_inference.environment.models import CheckResult, VerificationReport

    accepted = EnvironmentManifest.model_validate(manifest or reviewed_metadata()["environment_manifest"])
    commands = list(accepted.checks)
    for service in accepted.services:
        commands.extend([service.start, service.health, service.stop])
        if service.init:
            commands.append(service.init)
    report = VerificationReport(manifest_digest=accepted.digest(), results=[
        CheckResult(id=command.id, status="pass", required=command.required, exit_code=0,
                    manifest_digest=accepted.digest()) for command in commands])
    return (report.model_dump_json() + "\n").encode()

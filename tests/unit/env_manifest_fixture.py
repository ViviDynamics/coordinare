"""Real reviewed contracts used by environment transport and security fixtures."""
from __future__ import annotations

import hashlib

from agent.performer.tests.source_contract_fixture import current_review_fixture
from coordinare_service_inference.environment.models import (
    EnvironmentManifest,
    ExecutionCapabilities,
)


def fixture_execution_source(commit='a'*40, *, files=None):
    """Actual baseline tree identity for fake external verifier transports."""
    from coordinare_service_inference.environment.execution_source import (
        SourceEntry,
        source_identity,
    )
    contents = files if files is not None else {'tools.plan': 'baseline'}
    entries = []
    for path, text in contents.items():
        body = text.encode()
        entries.append(SourceEntry(path=path, mode='100644', size=len(body),
            object_id=hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()))
    return source_identity(commit, entries)


def reviewed_metadata(*, capabilities=None, checks=None, install=None, services=None, activation=None, source_commit="a" * 40,
                      symphony_name="sym", inputs=None, empty_reason="Image baseline is sufficient"):
    manifest = EnvironmentManifest.model_validate({
        "inventory_digest": "d" * 64, "capabilities_digest": capabilities.digest() if capabilities else "c" * 64,
        "schema_version": 1, "symphony_name": symphony_name, "source_commit": source_commit,
        "inputs": inputs if inputs is not None else {"tools.plan": hashlib.sha256(b"baseline").hexdigest()},
        "requirements": [], "install": install or [],
        "checks": checks or [{"id": "baseline", "argv": ["true"]}],
        "activation": activation or {}, "services": services or [],
        "empty_reason": empty_reason, "policy_version": "351-v1",
    })
    return {"environment_execution_source":fixture_execution_source(source_commit).model_dump(),
            "environment_manifest": manifest.model_dump(), "environment_manifest_digest": manifest.digest(),
            "environment_assessment": current_review_fixture(manifest, {"inventory_digest": "d" * 64, "capabilities_digest": capabilities.digest() if capabilities else "c" * 64, "schema_version": 1, "manifest_digest": manifest.digest(),
                "source_commit": source_commit, "verdict": "accept", "rationale": "Read source requirements",
                "sources": list(manifest.inputs), "inputs": dict(manifest.inputs), "policy_version": "351-v1"})}


def reviewed_cache(cache, *, files=None, capabilities=None, source_commit="a" * 40):
    """Bind a persisted cache fixture to actual inspected source bytes."""
    contents = files if files is not None else {"tools.plan": "baseline"}
    inputs = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in contents.items()}
    if capabilities is None:
        capabilities = ExecutionCapabilities(schema_version=1, image_ref="trusted/global@sha256:" + "a" * 64,
            platform="linux/x86_64", cache_mount_path=f"/devenv/{cache.sanitised_name}",
            install_network="enabled", verify_network="disabled")
    cache.environment_capabilities = capabilities.model_dump()
    metadata = reviewed_metadata(inputs=inputs, capabilities=capabilities, source_commit=source_commit, symphony_name=cache.symphony_name)
    cache.environment_execution_source = fixture_execution_source(source_commit, files=contents).model_dump()
    cache.environment_manifest = metadata["environment_manifest"]
    cache.environment_assessment = metadata["environment_assessment"]
    cache.bootstrap_commit_sha = source_commit
    import json

    cache.environment_verification = json.loads(verification_output(cache.environment_manifest,
        execution_source=fixture_execution_source(source_commit, files=contents)))
    cache.environment_verification["image_ref"] = "trusted/global@sha256:" + "a" * 64
    return cache


def verification_output(manifest=None, *, execution_source=None):
    """Fake container's parent-collected clean-run JSON, bound to its accepted contract."""
    from coordinare_service_inference.environment.models import CheckResult, VerificationReport

    accepted = EnvironmentManifest.model_validate(manifest or reviewed_metadata()["environment_manifest"])
    commands = list(accepted.checks)
    for service in accepted.services:
        commands.extend([service.start, service.health, service.stop])
        if service.init:
            commands.append(service.init)
    report = VerificationReport(platform="linux/x86_64", manifest_digest=accepted.digest(),
        execution_source_digest=(execution_source or fixture_execution_source(accepted.source_commit)).digest(), results=[
        CheckResult(id=command.id, status="pass", required=command.required, exit_code=0,
                    manifest_digest=accepted.digest()) for command in commands])
    return (report.model_dump_json() + "\n").encode()


def record_verified_cache_fixture(cache):
    """Model the trusted verifier/freeze callback in lifecycle unit tests only.

    These tests replace external execution, so they must supply its contract-bound
    evidence explicitly before simulating a successful completion notification.
    The real-repository validation harness must never call this fixture helper.
    """
    import json

    from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
    if cache.environment_execution_source is None:
        cache.environment_execution_source=fixture_execution_source(cache.bootstrap_commit_sha).model_dump()
    cache.environment_verification = json.loads(verification_output(cache.environment_manifest,
        execution_source=ExecutionSourceIdentity.model_validate(cache.environment_execution_source)))
    cache.environment_verification["image_ref"] = "trusted/global@sha256:" + "a" * 64
    cache.provenance_digest = cache.provenance_digest or "f" * 64


def manifest_proposal(manifest):
    """Convert a full persisted fixture to model-owned declarations, retaining unknown extras."""
    daemon_fields = {'schema_version', 'policy_version', 'symphony_name', 'source_commit',
                     'inputs', 'inventory_digest', 'capabilities_digest'}
    return {key: value for key, value in manifest.items() if key not in daemon_fields}


def review_proposal(assessment):
    """Convert a full persisted assessment fixture to its model-owned judgment."""
    daemon_fields = {'schema_version', 'policy_version', 'symphony_name', 'source_commit', 'inputs',
                     'inventory_digest', 'capabilities_digest', 'manifest_digest', 'finding_ledger', 'finding_ledger_digest'}
    return {'findings': [], 'decisions': [], 'capability_unavailable': False,
            **{key: value for key, value in assessment.items() if key not in daemon_fields}}


def decode_test_execution_frame(data):
    """Translate trusted binary stdin at fake Docker I/O, validating both frames.

    Legacy verifier tests inspect/execute the daemon script. Full source restore
    itself is exercised through the real shared CLI in execution-source tests.
    """
    import json

    if data is None:
        return None
    try:
        raw, body = data.split(b"\n", 1)
        header = json.loads(raw)
    except (ValueError, TypeError):
        return data
    if not isinstance(header, dict) or "payload_bytes" not in header:
        return data
    payload, script = body[: header["payload_bytes"]], body[header["payload_bytes"] :]
    assert (
        len(payload) == header["payload_bytes"]
        and hashlib.sha256(payload).hexdigest() == header["payload_sha256"]
    )
    assert (
        len(script) == header["script_bytes"]
        and hashlib.sha256(script).hexdigest() == header["script_sha256"]
    )
    return script


def patch_clean_source_transport(monkeypatch, tmp_path, *, files=None):
    """Stub source provider I/O with a real validated binary bundle."""
    from coordinare_service_inference.environment.execution_source import (
        ExecutionSourceBundle,
        ExecutionSourceIdentity,
    )

    from tests.unit.test_353_execution_source import bundle_file

    async def source(state, name, cache, *, destination, deadline):
        destination.mkdir(parents=True)
        contents = (
            {p: (text.encode(), "100644") for p, text in files.items()}
            if files is not None
            else {"tools.plan": (b"baseline", "100644")}
        )
        path, identity = bundle_file(destination, contents, commit=cache.bootstrap_commit_sha)
        return ExecutionSourceBundle(
            ExecutionSourceIdentity.model_validate(identity),
            path,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
        )

    monkeypatch.setattr(
        "coordinare.services.env_cache.ensure_execution_source", source,
    )

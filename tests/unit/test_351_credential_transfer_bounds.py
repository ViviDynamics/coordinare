"""Large credentials cross verifier startup only in bounded encoded fragments."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.environment_verification import verification_environment
from coordinare.services.kubernetes_verifier import (
    VerifierSpec,
    build_verifier_credentials_secret,
    build_verifier_pod_manifest,
)
from tests.unit.env_manifest_fixture import reviewed_cache
from tests.unit.test_351_environment_runner import contract

PREFIX = "COORDINARE_ENV_SECRET_VALUES"
LARGE_SECRET = "large-fixture-value" * 5600  # 100800 bytes; fits Linux's per-value limit.
VALUES = {"UNUSUAL_DSN": LARGE_SECRET, "PRIVATE_DSN": "small-fixture-secret"}
ENCODED = base64.b64encode(json.dumps(VALUES).encode()).decode()


def _spec():
    return VerifierSpec(image="trusted:latest", cache_claim="cache", cache_subpath="suite",
        mount_path="/devenv", verify_text="true", project_files={}, transient_credentials=ENCODED)


def _kubernetes_environment():
    pod = build_verifier_pod_manifest(_spec(), name="verify", namespace="default")
    secret = build_verifier_credentials_secret(_spec(), name="verify", namespace="default", owner=None, pod_uid="pod-uid")
    env = {}
    for entry in pod["spec"]["containers"][0]["env"]:
        if entry["name"].startswith(PREFIX):
            env[entry["name"]] = (entry["value"] if "value" in entry else
                base64.b64decode(secret["data"][entry["valueFrom"]["secretKeyRef"]["key"]]).decode())
    return pod, secret, env


@pytest.mark.asyncio
async def test_docker_and_kubernetes_large_credentials_use_identical_bounded_fragments(tmp_path):
    github = SimpleNamespace(get_file_content=AsyncMock(return_value="\n".join(f"{key}={value}" for key, value in VALUES.items())))
    effective = SimpleNamespace(github_org="org", project_name="suite")
    symphony = SimpleNamespace(name="suite", test_env=SimpleNamespace(repo_path="test.env", host_path=None),
                              effective_config=lambda _: effective)
    cache = reviewed_cache(EnvCacheState(symphony_name="suite", sanitised_name="suite", cache_dir=tmp_path))
    script = await verification_environment({"coordinare_config": SimpleNamespace(global_config=effective,
        symphonies=[symphony]), "github_service": github}, "suite", cache, ())
    docker_env = dict(shlex.split(line)[1].split("=", 1) for line in script.splitlines())
    pod, secret, kubernetes_env = _kubernetes_environment()
    assert all(len((key + "=" + value).encode()) < 128 * 1024 for key, value in docker_env.items())
    assert docker_env == kubernetes_env
    assert ENCODED not in json.dumps(pod)
    assert LARGE_SECRET not in json.dumps(pod)
    assert len(secret["data"]) > 1


def _run_runner(tmp_path, envelope):
    digest = hashlib.sha256(LARGE_SECRET.encode()).hexdigest()
    check = (
        "import hashlib,os;"
        f"assert hashlib.sha256(os.environ['UNUSUAL_DSN'].encode()).hexdigest()=={digest!r};"
        f"assert not any(key.startswith({PREFIX!r}) for key in os.environ);"
        "print('y'*100800);print(os.environ['PRIVATE_DSN'])"
    )
    manifest = contract([{"id": "baseline", "argv": [sys.executable, "-c", check]}])
    return subprocess.run([sys.executable, "-I", "-m", "coordinare_service_inference.environment.runner",
        "--manifest", manifest.canonical_json(), "--cache", str(tmp_path), "--repo", str(tmp_path),
        "--scratch", str(tmp_path)], env={**os.environ, **envelope}, text=True,
        capture_output=True, check=False, timeout=15)


def test_runner_restores_chunked_credentials_and_removes_fragments_before_checks(tmp_path):
    chunks = [ENCODED[offset:offset + 65536] for offset in range(0, len(ENCODED), 65536)]
    envelope = {PREFIX + "_COUNT": str(len(chunks)), **{f"{PREFIX}_{index}": chunk for index, chunk in enumerate(chunks)}}
    result = _run_runner(tmp_path, envelope)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"]
    assert "small-fixture-secret" not in result.stdout


def test_kubernetes_secret_transfer_preserves_total_admission_bound():
    with pytest.raises(ValueError, match="transfer bound"):
        build_verifier_credentials_secret(replace(_spec(), transient_credentials="x" * (384 * 1024 + 1)),
            name="verify", namespace="default", owner=None, pod_uid="pod-uid")


@pytest.mark.parametrize("name", [PREFIX + "_COUNT", PREFIX + "_0"])
@pytest.mark.parametrize("location", ["command", "activation"])
def test_manifest_cannot_override_credential_transport_fragments(name, location):
    check = {"id": "baseline", "argv": ["true"]}
    options = {}
    if location == "command":
        check["env"] = {name: "${" + name + "}"}
    else:
        options["activation"] = {"env": {name: "${" + name + "}"}}
    with pytest.raises(ValidationError, match="reserved"):
        contract([check], **options)


@pytest.mark.parametrize("envelope", [
    {PREFIX + "_COUNT": "7"},
    {PREFIX + "_COUNT": "1"},
    {PREFIX + "_UNEXPECTED": "untrusted-fragment"},
    {PREFIX: base64.b64encode(json.dumps({PREFIX + "_COUNT": "1"}).encode()).decode()},
])
def test_runner_refuses_malformed_or_reserved_secret_controls(tmp_path, envelope):
    result = _run_runner(tmp_path, envelope)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "invalid transient secret environment payload" in result.stderr

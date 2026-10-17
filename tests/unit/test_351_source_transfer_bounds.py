"""Discovery must admit only inputs both clean verifier transports can carry."""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services.environment_discovery import discover_and_review
from coordinare.services.environment_verification import source_setup
from coordinare.services.kubernetes_verifier import (
    PROJECT_FILES_MAX_BYTES,
    VerifierSpec,
    build_verifier_pod_manifest,
)


async def _discover(files: dict[str, str], reviewer_extra: str = ""):
    paths = list(files)
    manifest = {
        "schema_version": 1, "policy_version": "351-v1", "symphony_name": "suite",
        "source_commit": "a" * 40,
        "inputs": {path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()},
        "requirements": [{"id": "tool", "description": "tool is usable", "source": paths[0],
                          "checks": ["version"], "required": True}],
        "install": [], "checks": [{"id": "version", "argv": ["tool", "--version"], "required": True}],
        "activation": {"path": [], "env": {}}, "services": [], "empty_reason": None,
    }
    replies = [{"tool": "read_file", "path": path} for path in paths]
    replies.append({"manifest": manifest})
    replies.extend({"tool": "read_file", "path": path} for path in paths)
    if reviewer_extra:
        replies.append({"tool": "read_file", "path": "additional-context"})
    replies.append({"assessment": {
        "schema_version": 1, "policy_version": "351-v1", "source_commit": "a" * 40,
        "manifest_digest": EnvironmentManifest.model_validate(manifest).digest(),
        "verdict": "accept", "rationale": "Every documented requirement has a check", "sources": paths,
    }})
    responses = iter(replies)

    async def chat(messages):
        return json.dumps(next(responses))

    async def fetch(path):
        return reviewer_extra if path == "additional-context" else files[path]

    return await discover_and_review(symphony_name="suite", commit="a" * 40,
        paths=[*paths, "additional-context"], fetch=fetch, chat=chat)


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [
    {"first": "a" * (150 * 1024), "second": "b" * (150 * 1024)},
    {"escaped": "\0" * (50 * 1024)},
])
async def test_discovery_accepts_aggregate_and_escaped_inputs_within_shared_document_limit(files):
    # Raw and canonical document admission are shared; platform transport is separate.
    manifest, assessment = await _discover(files)
    assert set(manifest.inputs) == set(files)
    assert assessment.verdict == "accept"


@pytest.mark.asyncio
async def test_reviewer_can_read_more_context_than_the_verifier_transfer_budget():
    manifest, assessment = await _discover({"evidence": "tool must be usable"}, "x" * (256 * 1024))
    assert assessment.verdict == "accept"
    assert list(manifest.inputs) == ["evidence"]
    assert "additional-context" in assessment.inputs


def test_docker_and_kubernetes_use_the_same_bounded_unicode_serialization():
    files = {"evidence": "é" * (100 * 1024)}
    setup_command = source_setup(files).split("$(", 1)[1].split(")\nexport", 1)[0]
    program = setup_command.split("\n", 1)[1].rsplit("COORDINARE_SOURCE_PY", 1)[0]
    decode_call = next(node for node in ast.walk(ast.parse(program))
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr == "b64decode")
    docker_payload = base64.b64decode(ast.literal_eval(decode_call.args[0]))
    pod = build_verifier_pod_manifest(VerifierSpec(image="trusted:latest", cache_claim="cache",
        cache_subpath="suite", mount_path="/devenv", verify_text="true", project_files=files),
        name="verify", namespace="default")
    env = {entry["name"]: entry["value"] for entry in pod["spec"]["containers"][0]["env"]}
    kubernetes_payload = base64.b64decode("".join(env[f"PROJECT_FILES_B64_{index}"]
        for index in range(int(env["PROJECT_FILES_B64_COUNT"]))))
    assert len(docker_payload) <= PROJECT_FILES_MAX_BYTES
    assert docker_payload == kubernetes_payload
    assert json.loads(docker_payload) == files


def test_docker_source_setup_fits_linux_individual_argument_limit():
    setup_command = source_setup({"evidence": "x" * (200 * 1024)}).split("$(", 1)[1].split(")\nexport", 1)[0]
    # Linux limits each argv/env string to 128 KiB, even when ARG_MAX is larger.
    invocation = setup_command.split("\n", 1)[0] if "<<" in setup_command else setup_command
    arguments = shlex.split(invocation)
    assert all(len(argument.encode()) < 128 * 1024 for argument in arguments)


def test_kubernetes_source_and_verify_payloads_fit_linux_individual_environment_limit():
    pod = build_verifier_pod_manifest(VerifierSpec(image="trusted:latest", cache_claim="cache",
        cache_subpath="suite", mount_path="/devenv", verify_text="#" * (200 * 1024),
        project_files={"evidence": "x" * (200 * 1024)}, activate_text="#" * (200 * 1024), walk_only=True),
        name="verify", namespace="default")
    env = pod["spec"]["containers"][0]["env"]
    assert all(len((entry["name"] + "=" + entry["value"]).encode()) < 128 * 1024 for entry in env)


def test_docker_heredoc_materializes_maximum_source_without_content_interpolation():
    text = "x" * (PROJECT_FILES_MAX_BYTES - len('{"evidence": ""}'))
    script = source_setup({"evidence": text}).replace("/usr/local/bin/python", shlex.quote(sys.executable))
    result = subprocess.run(["bash"], input=script + 'printf "%s" "$COORDINARE_REPO_DIR"\n',
        text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    root = Path(result.stdout)
    try:
        assert (root / "evidence").read_text() == text
    finally:
        shutil.rmtree(root)


def test_kubernetes_setup_reassembles_maximum_source_and_verify_text(tmp_path):
    source = "é" * ((PROJECT_FILES_MAX_BYTES - len('{"evidence": ""}')) // 2)
    verify = "#" * PROJECT_FILES_MAX_BYTES
    pod = build_verifier_pod_manifest(VerifierSpec(image="trusted:latest", cache_claim="cache",
        cache_subpath="suite", mount_path="/devenv", verify_text=verify, project_files={"evidence": source}),
        name="verify", namespace="default")
    env = {entry["name"]: entry["value"] for entry in pod["spec"]["containers"][0]["env"]}
    result = subprocess.run([sys.executable, "-I", "-c", env["SETUP_PY"], str(tmp_path)],
        env=env, text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "repo" / "evidence").read_text() == source
    assert (tmp_path / "verify.sh").read_text() == verify


def test_kubernetes_walker_reassembles_large_activation_without_child_exec(tmp_path):
    activation = "#" * (200 * 1024)
    (tmp_path / "activate.sh").write_text(activation)
    pod = build_verifier_pod_manifest(VerifierSpec(image="trusted:latest", cache_claim="cache",
        cache_subpath="suite", mount_path=str(tmp_path), verify_text="", project_files={},
        activate_text=activation, walk_only=True), name="verify", namespace="default")
    env = {entry["name"]: entry["value"] for entry in pod["spec"]["containers"][0]["env"]}
    # Use the current interpreter's installed performer, as the trusted image does.
    result = subprocess.run([sys.executable, "-P", "-c", env["WALK_SETUP_PY"]],
        env={**env, "PATH": os.environ["PATH"]}, text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.removeprefix("COORDINARE_PROVENANCE="))
    assert summary["inputs"]["activate_sh"] == activation

"""Execute trusted source reconstruction instead of inspecting encoded source text."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys

import pytest

from coordinare.services.environment_verification import source_setup
from coordinare.services.kubernetes_verifier import (
    VerifierSpec,
    build_verifier_pod_manifest,
    build_verifier_source_secrets,
)


def files_and_inputs():
    files = {"a/evidence": "é" * 100_000, "b/evidence": "y" * 200_000}
    return files, {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}


def test_docker_large_payload_uses_stdin(tmp_path):
    files, inputs = files_and_inputs()
    setup = source_setup(files, expected_inputs=inputs).replace("/usr/local/bin/python", shlex.quote(sys.executable))
    # The trusted program reads the heredoc from bash stdin, never argv/env.
    script = setup + 'cp -R "$COORDINARE_REPO_DIR" "$OUTPUT"\nrm -rf "$COORDINARE_REPO_DIR"\n'
    result = subprocess.run(["bash", "-s"], input=script, env={**os.environ, "OUTPUT": str(tmp_path / "out")},
        text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out/a/evidence").read_bytes() == files["a/evidence"].encode()
    assert (tmp_path / "out/b/evidence").read_bytes() == files["b/evidence"].encode()


def source_spec():
    files, inputs = files_and_inputs()
    return VerifierSpec(image="trusted/image@sha256:" + "a" * 64, cache_claim="claim", cache_subpath="sym",
        mount_path="/devenv/sym", verify_text="true", project_files=files, source_inputs=inputs,
        platform="linux/x86_64")


def test_kubernetes_source_payload_not_in_pod_env(tmp_path):
    spec = source_spec()
    pod = build_verifier_pod_manifest(spec, name="attempt", namespace="ns", owner="owner")
    serialized = json.dumps(pod)
    assert "PROJECT_FILES_B64" not in serialized
    assert "é" * 50 not in serialized
    assert pod["spec"]["nodeSelector"] == {"kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64"}
    source_dir = tmp_path / "payload"
    source_dir.mkdir()
    secrets = build_verifier_source_secrets(spec, name="attempt", namespace="ns", owner="owner", pod_uid="pod-uid")
    for secret in secrets:
        for name, encoded in secret["data"].items():
            (source_dir / name).write_bytes(base64.b64decode(encoded))
    init = pod["spec"]["initContainers"][0]
    command = [sys.executable, *init["command"][1:]]
    command[-2:] = [str(source_dir), str(tmp_path / "repo")]
    result = subprocess.run(command, text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "repo/a/evidence").read_bytes() == spec.project_files["a/evidence"].encode()
    mounts = pod["spec"]["containers"][0]["volumeMounts"]
    assert all(mount["name"] != "source-payload" for mount in mounts)


def test_chunk_secret_and_pod_size_bounds():
    spec = source_spec()
    secrets = build_verifier_source_secrets(spec, name="attempt", namespace="ns", owner="owner", pod_uid="pod-uid")
    for secret in secrets:
        assert secret["immutable"] is True
        assert len(json.dumps(secret).encode()) < 900 * 1024
        assert len([key for key in secret["data"] if key.startswith("chunk-")]) <= 8
        assert secret["metadata"]["ownerReferences"][0]["uid"] == "pod-uid"
    pod = build_verifier_pod_manifest(spec, name="attempt", namespace="ns", owner="owner")
    assert len(json.dumps(pod).encode()) < 900 * 1024
    assert all(len((item["name"] + "=" + item.get("value", "")).encode()) < 128 * 1024
               for item in pod["spec"]["containers"][0]["env"])


def test_corrupt_mounted_payload_stops_before_source_materialization(tmp_path):
    spec = source_spec()
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    for secret in build_verifier_source_secrets(spec, name="test", namespace="ns", owner="owner", pod_uid="uid"):
        for name, value in secret["data"].items():
            (payload_dir / name).write_bytes(base64.b64decode(value))
    chunk = payload_dir / "chunk-0000"
    chunk.write_bytes(b"!" + chunk.read_bytes()[1:])
    result = subprocess.run([sys.executable, "-I", "-m", "coordinare_service_inference.environment.source_payload",
        str(payload_dir), str(tmp_path / "repo")], text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode != 0
    assert not (tmp_path / "repo").exists()


@pytest.mark.parametrize(("count", "text_size"), [(110, 1), (160, 4096), (256, 4096)])
def test_unicode_paths_fit_shared_admission_and_kubernetes_roundtrip(tmp_path, count, text_size):
    from dataclasses import replace

    from coordinare_service_inference.environment.source_payload import (
        encode_sources,
        restore_source_directory,
    )

    prefix = "/".join(["é" * 60] * 6)
    files = {prefix + f"/evidence-{index:04}": "x" * text_size for index in range(count)}
    inputs = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()}
    assert len(json.dumps(inputs, ensure_ascii=False).encode()) < 256 * 1024
    encode_sources(files)
    spec = replace(source_spec(), project_files=files, source_inputs=inputs)
    payload = tmp_path / "payload"
    payload.mkdir()
    secrets = build_verifier_source_secrets(spec, name="test", namespace="ns", owner="owner", pod_uid="uid")
    pod = build_verifier_pod_manifest(spec, name="test", namespace="ns", owner="owner")
    assert len(pod["spec"]["volumes"][-1]["projected"]["sources"]) == len(secrets)
    for secret in secrets:
        assert len(json.dumps(secret).encode()) < 900 * 1024
        for key, value in secret["data"].items():
            (payload / key).write_bytes(base64.b64decode(value))
    restore_source_directory(payload, tmp_path / "restored")
    for path, content in files.items():
        assert (tmp_path / "restored" / path).read_text() == content

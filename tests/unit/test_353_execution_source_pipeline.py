"""Full-tree runtime integration must not fall back to model review files."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys

import pytest

from tests.unit.test_353_execution_source import COMMIT, FILES, bundle_file


def bundle_fixture(tmp_path, files=None):
    from coordinare_service_inference.environment.execution_source import (
        ExecutionSourceBundle,
        ExecutionSourceIdentity,
    )

    path, identity = bundle_file(tmp_path, files)
    return ExecutionSourceBundle(
        ExecutionSourceIdentity.model_validate(identity),
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_size,
    )


def transport_api():
    assert importlib.util.find_spec("coordinare.services.environment_source_transport") is not None
    return importlib.import_module("coordinare.services.environment_source_transport")


def test_framed_clean_verifier_sees_unreviewed_binary_source(tmp_path):
    transport = transport_api()
    bundle = bundle_fixture(tmp_path)
    script = 'test -f "$COORDINARE_REPO_DIR/src/main" && test -x "$COORDINARE_REPO_DIR/bin/tool" && cat "$COORDINARE_REPO_DIR/src/main"'
    frame = transport.execution_input(
        bundle,
        script,
        review_inputs={"Buildfile": hashlib.sha256(FILES["Buildfile"][0]).hexdigest()},
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "coordinare_service_inference.environment.execution_source_stdin",
        ],
        input=frame,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"app\n"
    assert (
        b"app\n"
        not in json.dumps(transport.execution_header(bundle, script, review_inputs={})).encode()
    )


@pytest.mark.parametrize("change", ["missing", "corrupt", "extra", "header"])
def test_invalid_binary_frame_never_runs_project_script(tmp_path, change):
    transport = transport_api()
    bundle = bundle_fixture(tmp_path)
    sentinel = tmp_path / "ran"
    frame = transport.execution_input(bundle, f"touch {sentinel}", review_inputs={})
    header, body = frame.split(b"\n", 1)
    if change == "missing":
        frame = header + b"\n" + body[:-2]
    elif change == "extra":
        frame += b"extra"
    elif change == "corrupt":
        frame = header + b"\n" + b"bad" + body[3:]
    else:
        frame = b'{"payload_bytes":999999999999}\n' + body
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "coordinare_service_inference.environment.execution_source_stdin",
        ],
        input=frame,
        capture_output=True,
    )
    assert result.returncode != 0
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_probe_sees_unreviewed_application_directory(tmp_path, monkeypatch):
    from coordinare.services import environment_probe_executor as executor
    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_probes import ProbeCommandResult
    from tests.unit.test_353_environment_probes import plan

    bundle = bundle_fixture(tmp_path)
    p = plan().model_copy(
        update={
            "source_commit": COMMIT,
            "inputs": {"Buildfile": hashlib.sha256(FILES["Buildfile"][0]).hexdigest()},
        },
    )

    async def identity(image):
        return image, p.capabilities.platform

    async def command(plan, roots, command, deadline):
        assert (roots[0] / "src/main").read_bytes() == b"app\n"
        assert (roots[0] / "asset.bin").read_bytes() == b"\x00\xff\x80"
        return ProbeCommandResult(
            id=command["id"],
            status="completed",
            exit_code=0,
            output="",
            truncated=False,
        )

    monkeypatch.setattr(executor, "_docker_identity", identity)
    monkeypatch.setattr(executor, "_command_container", command)
    report = await executor.execute_environment_probe(
        p,
        state={"probe_backend": "docker"},
        source_bundle=bundle,
        budget=AttemptBudget(),
    )
    assert report.cleanup_confirmed and report.results[0].exit_code == 0


def test_kubernetes_restores_same_full_tree_identity_as_docker(tmp_path):
    import base64

    from coordinare.services.kubernetes_verifier import (
        VerifierSpec,
        build_verifier_pod_manifest,
        build_verifier_source_secrets,
    )

    bundle = bundle_fixture(tmp_path)
    spec = VerifierSpec(
        image="trusted/image@sha256:" + "a" * 64,
        cache_claim="claim",
        cache_subpath="sym",
        mount_path="/devenv/sym",
        verify_text="true",
        project_files={},
        source_inputs={"Buildfile": hashlib.sha256(FILES["Buildfile"][0]).hexdigest()},
        execution_source=bundle,
    )
    pod = build_verifier_pod_manifest(spec, name="attempt", namespace="ns")
    secrets = build_verifier_source_secrets(
        spec,
        name="attempt",
        namespace="ns",
        owner="owner",
        pod_uid="uid",
    )
    assert secrets and all(s["metadata"]["ownerReferences"][0]["uid"] == "uid" for s in secrets)
    assert all(len(json.dumps(s).encode()) < 900 * 1024 for s in secrets)
    assert "build src/main" not in json.dumps(pod)
    projected = tmp_path / "projected"
    projected.mkdir()
    for secret in secrets:
        for name, value in secret["data"].items():
            (projected / name).write_bytes(base64.b64decode(value))
    init = pod["spec"]["initContainers"][0]
    result = subprocess.run(
        [sys.executable, *init["command"][1:-2], str(projected), str(tmp_path / "restored")],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert (tmp_path / "restored/asset.bin").read_bytes() == b"\x00\xff\x80"
    assert (tmp_path / "restored/src/main").read_bytes() == b"app\n"


def test_elm_sized_source_metadata_and_bytes_fit_kubernetes_objects(tmp_path):
    import base64

    from coordinare_service_inference.environment.execution_source_restore import (
        restore_projected_source,
    )

    from coordinare.services.kubernetes_verifier import (
        VerifierSpec,
        build_verifier_pod_manifest,
        build_verifier_source_secrets,
    )

    total = 77_426_246
    count = 6616
    largest = 1_172_711
    size, remainder = divmod(total - largest, count - 1)
    files = {"src/file0000": (b"B" * largest, "100644")}
    regular = b"x" * size
    plus = b"x" * (size + 1)
    files.update(
        {
            f"src/file{i:04}": (plus if i <= remainder else regular, "100644")
            for i in range(1, count)
        },
    )
    bundle = bundle_fixture(tmp_path, files)
    assert bundle.identity.file_count == 6616 and bundle.identity.expanded_bytes == 77_426_246
    spec = VerifierSpec(
        image="trusted/image@sha256:" + "a" * 64,
        cache_claim="claim",
        cache_subpath="sym",
        mount_path="/devenv/sym",
        verify_text="true",
        project_files={},
        source_inputs={},
        execution_source=bundle,
    )
    pod = build_verifier_pod_manifest(spec, name="large", namespace="ns")
    secrets = build_verifier_source_secrets(
        spec,
        name="large",
        namespace="ns",
        owner="owner",
        pod_uid="uid",
    )
    assert len(json.dumps(pod).encode()) < 900 * 1024
    projected = tmp_path / "projection"
    projected.mkdir()
    for secret in secrets:
        assert len(json.dumps(secret).encode()) < 900 * 1024
        assert len([k for k in secret["data"] if k.startswith("chunk-")]) <= 8
        for key, value in secret["data"].items():
            (projected / key).write_bytes(base64.b64decode(value))
    restore_projected_source(projected, tmp_path / "large-repo")
    assert len((tmp_path / "large-repo/src/file0000").read_bytes()) == 1_172_711
    assert (tmp_path / "large-repo/src/file6615").read_bytes() == regular


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_create", [False, True])
async def test_binary_source_secrets_and_local_artifact_cleaned(tmp_path, failed_create):
    from dataclasses import replace

    from coordinare.services.kubernetes_runtime import KubernetesRuntime
    from tests.unit.test_353_payload_lifecycle import SourceCore
    from tests.unit.test_353_source_transport import source_spec

    core = SourceCore(fail_at=1 if failed_create else None)
    runtime = KubernetesRuntime(namespace="ns", cache_claim="claim", core_v1=core, owner="owner")
    owned = tmp_path / "owned"
    owned.mkdir()
    bundle = bundle_fixture(owned)
    spec = replace(
        source_spec(),
        project_files={},
        source_inputs={},
        execution_source=bundle,
        execution_source_workspace=owned,
    )

    async def finish(*args, **kwargs):
        return True, "verified"

    async def remove(name):
        return True

    runtime._await_verifier = finish
    runtime._remove_verifier = remove
    passed, _ = await runtime.run_verifier(slug="sym", spec=spec)
    assert passed is (None if failed_create else True)
    assert not owned.exists()
    assert not core.secrets

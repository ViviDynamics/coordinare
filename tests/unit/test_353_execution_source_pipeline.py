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
            "execution_source_digest": bundle.identity.digest(),
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


def test_readiness_rejects_missing_or_mismatched_execution_source(tmp_path):
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_acceptance import execution_report
    from tests.unit.env_manifest_fixture import reviewed_cache, verification_output

    cache = reviewed_cache(
        EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path / "cache"),
    )
    evidence = json.loads(verification_output(cache.environment_manifest))
    evidence.pop("execution_source_digest", None)
    with pytest.raises(ValueError, match="source"):
        execution_report(json.dumps(evidence), cache)
    bundle = bundle_fixture(tmp_path)
    cache.environment_execution_source = bundle.identity.model_dump()
    evidence["execution_source_digest"] = "0" * 64
    with pytest.raises(ValueError, match="source"):
        execution_report(json.dumps(evidence), cache)


def test_complete_execution_source_is_bound_into_readiness(tmp_path):
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_acceptance import execution_report
    from tests.unit.env_manifest_fixture import reviewed_cache, verification_output

    cache = reviewed_cache(
        EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path / "cache"),
    )
    bundle = bundle_fixture(tmp_path)
    cache.environment_execution_source = bundle.identity.model_dump()
    evidence = json.loads(verification_output(cache.environment_manifest))
    evidence["execution_source_digest"] = bundle.identity.digest()
    assert execution_report(json.dumps(evidence), cache).passed


@pytest.mark.asyncio
async def test_installer_checkout_identity_checks_actual_files_not_only_git_objects(tmp_path):
    import importlib

    assert (
        importlib.util.find_spec(
            "coordinare_service_inference.environment.execution_source_checkout",
        )
        is not None
    )
    module = importlib.import_module(
        "coordinare_service_inference.environment.execution_source_checkout",
    )
    repo = tmp_path / "checkout"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
        ).stdout

    git("init")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.com")
    (repo / "source").write_bytes(b"original")
    git("add", "source")
    git("commit", "-m", "fixture")
    commit = git("rev-parse", "HEAD").decode().strip()
    identity = await module.checkout_identity(
        repo,
        commit=commit,
        review_inputs={"source": hashlib.sha256(b"original").hexdigest()},
    )
    assert identity.source_commit == commit
    (repo / "source").write_bytes(b"modified")
    with pytest.raises(ValueError, match="source"):
        await module.checkout_identity(repo, commit=commit, review_inputs={})


def test_old_pending_source_journal_requires_explicit_restart(tmp_path):
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, journal_for
    from tests.unit.test_353_execution_repair import cache_fixture

    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    cache.environment_attempt["version"] = 3
    with pytest.raises(EnvironmentDiscoveryError, match="restart"):
        journal_for(cache)


def test_source_ownership_blocks_restart_and_does_not_renew_budget(tmp_path):
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, restart_environment_attempt
    from tests.unit.test_353_execution_repair import cache_fixture

    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    cache.environment_attempt["source_owner"] = str(tmp_path / "owned")
    with pytest.raises(EnvironmentDiscoveryError, match="cleanup"):
        restart_environment_attempt(cache)


@pytest.mark.asyncio
async def test_fresh_source_and_ordinary_tool_names_work_through_activation(tmp_path):
    from coordinare_service_inference.environment.execution_source_restore import (
        restore_execution_bundle,
    )
    from coordinare_service_inference.environment.models import EnvironmentManifest
    from coordinare_service_inference.environment.runner import run_manifest

    from tests.unit.env_manifest_fixture import reviewed_metadata

    bundle = bundle_fixture(tmp_path)
    installer = tmp_path / "installer"
    consumer = tmp_path / "consumer"
    restore_execution_bundle(
        bundle.payload_path,
        installer,
        expected=bundle.identity,
        review_inputs={},
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    program = "import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.mkdir();f=p/'fixture-consumer-tool';f.write_text('#!/bin/sh\\nprintf ready');f.chmod(0o700)"
    manifest = EnvironmentManifest.model_validate(
        reviewed_metadata(
            inputs={"Buildfile": hashlib.sha256(FILES["Buildfile"][0]).hexdigest()},
            install=[{"id": "tool", "argv": [sys.executable, "-c", program, "{cache}/bin"]}],
            activation={"path": ["{cache}/bin"]},
            checks=[
                {
                    "id": "usable",
                    "argv": [
                        "/bin/bash",
                        "-c",
                        'test "$(fixture-consumer-tool)" = ready && test "$(cat src/main)" = app',
                    ],
                },
            ],
        )["environment_manifest"],
    )
    assert (
        await run_manifest(manifest, cache=cache, repo=installer, scratch=scratch, install=True)
    ).passed
    (installer / "src/main").write_text("installer-only mutation")
    restore_execution_bundle(
        bundle.payload_path,
        consumer,
        expected=bundle.identity,
        review_inputs={},
    )
    assert (await run_manifest(manifest, cache=cache, repo=consumer, scratch=scratch)).passed


@pytest.mark.asyncio
async def test_independent_review_receives_activation_usability_requirement():
    from coordinare.services.environment_discovery import discover_and_review
    from tests.unit.test_353_discovery_repair import (
        CAPABILITIES,
        PATH,
        SOURCE,
        assessment,
        candidate,
    )

    async def fetch(path):
        return SOURCE

    reviewed = []

    async def chat(messages):
        context = json.loads(messages[1]["content"])
        if "manifest" in context:
            assert "ordinary downstream project commands" in messages[0]["content"]
            reviewed.append(True)
            return json.dumps(assessment(candidate()))
        if len(messages) == 2:
            return json.dumps({"tool": "read_file", "path": PATH})
        return json.dumps({"manifest": candidate()})

    await discover_and_review(
        symphony_name="suite",
        commit=COMMIT,
        paths=[PATH],
        fetch=fetch,
        chat=chat,
        capabilities=CAPABILITIES,
    )
    assert reviewed == [True]


@pytest.mark.asyncio
async def test_failed_source_checkpoint_never_acquires_or_executes(tmp_path):
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.env_cache import _verify_cache_runtime
    from tests.unit.env_manifest_fixture import reviewed_cache

    cache = reviewed_cache(
        EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path / "cache"),
    )

    async def failed_checkpoint():
        raise RuntimeError("checkpoint unavailable")

    state = {"env_cache": {"sym": cache}, "snapshot_checkpoint_fn": failed_checkpoint}
    with pytest.raises(RuntimeError, match="checkpoint"):
        await _verify_cache_runtime(state, "sym", None, ())
    assert cache.environment_source_owner is None
    assert not cache.cache_dir_ready


@pytest.mark.parametrize("owner_location", ["cache", "journal"])
def test_interrupted_source_ownership_cannot_reuse_saved_readiness(tmp_path, owner_location):
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_acceptance import saved_execution_accepted
    from tests.unit.env_manifest_fixture import reviewed_cache

    cache = reviewed_cache(
        EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path / "cache"),
    )
    assert saved_execution_accepted(cache)
    if owner_location == "cache":
        cache.environment_source_owner = str(tmp_path / "owned")
    else:
        cache.environment_attempt = {"source_owner": str(tmp_path / "owned")}
    assert not saved_execution_accepted(cache)

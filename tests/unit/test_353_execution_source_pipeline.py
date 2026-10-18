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
            id=command["id"], status="completed", exit_code=0, output="", truncated=False,
        )

    monkeypatch.setattr(executor, "_docker_identity", identity)
    monkeypatch.setattr(executor, "_command_container", command)
    report = await executor.execute_environment_probe(
        p, state={"probe_backend": "docker"}, source_bundle=bundle, budget=AttemptBudget(),
    )
    assert report.cleanup_confirmed and report.results[0].exit_code == 0

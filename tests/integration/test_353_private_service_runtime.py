"""Opt-in real runtime proof; skipped cases never establish the runtime gate."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.private_runtime_probe_process import collect_probe

pytestmark = [pytest.mark.integration, pytest.mark.timeout(3660)]


def _run_profile(tmp_path_factory: pytest.TempPathFactory, *, config_env: str) -> dict[str, Any]:
    if os.environ.get("COORDINARE_RUNTIME_INTEGRATION") != "1":
        pytest.skip("explicit private-runtime integration opt-in required")
    config = os.environ.get(config_env)
    assert config, f"set {config_env} to the bounded pinned fixture configuration"
    output = tmp_path_factory.mktemp("runtime-probe") / "evidence"
    inputs = json.loads(Path(config).read_text())
    process = subprocess.Popen(
        [sys.executable, "-m", "scripts.probe_private_service_runtime",
         "--config", config, "--output", str(output)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    completed = collect_probe(process, timeout=inputs["deadline_seconds"] + 60)
    evidence = json.loads((output / "result.json").read_text())
    evidence["driver_exit_code"] = completed.returncode
    return evidence


@pytest.fixture(scope="module")
def runtime_evidence(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run_profile(tmp_path_factory, config_env="COORDINARE_RUNTIME_PROBE_CONFIG")


def test_ordinary_commands_build_from_source_and_reach_offline_services(runtime_evidence: dict[str, Any]) -> None:
    report = runtime_evidence
    assert report["driver_exit_code"] == 0, report
    assert report["status"] == "fixture_passed"
    assert report["readiness_evidence"] is False
    for run in (report["installer"], report["clean_consumer"]):
        assert run["docker_response"] == "source-fixture-v1\n"
        assert run["compose_response"] == "source-fixture-v1\n"
        assert run["bind_mount_response"] == "fresh-bind-source\n"
        assert run["service_cleanup_exit_code"] == 0
    assert report["installer"]["source_root"] != report["clean_consumer"]["source_root"]
    assert report["clean_consumer"]["offline_rebuild_exit_code"] == 0
    assert report["clean_consumer"]["missing_image_exit_code"] != 0
    assert report["source_identity_verified"] is True


def test_guest_cannot_change_cache_escape_network_or_survive_cleanup(runtime_evidence: dict[str, Any]) -> None:
    report = runtime_evidence
    assert report["status"] == "fixture_passed", report
    assert report["clean_consumer"]["outer_cache_write_exit_code"] != 0
    assert report["clean_consumer"]["guest_cache_write_exit_code"] != 0
    assert report["immutable_artifacts_unchanged"] is True
    assert report["egress_denial"]["external"] is True
    assert report["egress_denial"]["carrier_host"] is True
    assert report["egress_denial"]["metadata"] is True
    assert report["parent_export_denied"] is True
    assert report["concurrent_port_isolation"] is True
    assert report["cancellation_cleanup_confirmed"] is True
    assert report["all_owned_resources_absent"] is True
    assert report["resource_exhaustion_cleanup_confirmed"] is True


def test_nested_linux_boots_inside_a_private_unprivileged_container(tmp_path_factory: pytest.TempPathFactory) -> None:
    report = _run_profile(tmp_path_factory, config_env="COORDINARE_RUNTIME_NESTED_PROBE_CONFIG")
    assert report["driver_exit_code"] == 0, report
    assert report["status"] == "nested_fixture_passed"
    assert report["readiness_evidence"] is False
    assert report["nested_kernel_execution_verified"] is True
    for phase in ("installer", "clean_consumer"):
        assert report[phase]["nested_kernel_exit_code"] == 0
        assert "NESTED_LINUX_WORKS" in report[phase]["nested_kernel_output"]
        assert report[phase]["nested_kernel_seconds"] > 0
    assert report["immutable_artifacts_unchanged"] is True
    assert report["all_owned_resources_absent"] is True


def test_buildkit_cache_mounts_and_frontend_work_in_an_offline_consumer(tmp_path_factory: pytest.TempPathFactory) -> None:
    if os.environ.get("COORDINARE_RUNTIME_INTEGRATION") != "1":
        pytest.skip("explicit private-runtime integration opt-in required")
    config = os.environ.get("COORDINARE_RUNTIME_BUILDKIT_PROBE_CONFIG")
    assert config, "bounded BuildKit fixture configuration required"
    image = json.loads(Path(config).read_text())["image"]
    plugin = subprocess.run(["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                             "--security-opt", "no-new-privileges", "--entrypoint", "/usr/bin/docker",
                             image, "buildx", "version"], capture_output=True, text=True, timeout=15)
    assert plugin.returncode == 0, plugin.stderr
    report = _run_profile(tmp_path_factory, config_env="COORDINARE_RUNTIME_BUILDKIT_PROBE_CONFIG")
    assert report["driver_exit_code"] == 0, report
    assert report["status"] == "buildkit_fixture_passed"
    assert report["readiness_evidence"] is False
    for phase in ("installer", "clean_consumer"):
        assert report[phase]["buildkit_cache_exit_code"] == 0
    assert report["immutable_artifacts_unchanged"] is True
    assert report["all_owned_resources_absent"] is True


def test_controller_cancellation_removes_the_actual_owned_guest(tmp_path: Path) -> None:
    if os.environ.get("COORDINARE_RUNTIME_INTEGRATION") != "1":
        pytest.skip("explicit private-runtime integration opt-in required")
    config = os.environ.get("COORDINARE_RUNTIME_PROBE_CONFIG")
    assert config, "bounded pinned configuration required"
    output = tmp_path / "evidence"
    process = subprocess.Popen([sys.executable, "-m", "scripts.probe_private_service_runtime",
                                "--config", config, "--output", str(output)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    owned: list[str] = []
    try:
        until = time.monotonic() + 30
        while time.monotonic() < until and process.poll() is None:
            ownership = output / "ownership.json"
            if ownership.exists():
                owned = json.loads(ownership.read_text())["containers"]
                running = subprocess.run(["docker", "top", owned[-1], "-eo", "pid,args"], capture_output=True, text=True, timeout=5)
                if running.returncode == 0 and "qemu-system-" in running.stdout:
                    break
            time.sleep(0.1)
        else:
            pytest.fail("controller never reached a live owned guest")
        process.terminate()
        completed = collect_probe(process, timeout=45)
        assert completed.returncode == 1
        report = json.loads((output / "result.json").read_text())
        assert report["status"] == "held"
        assert report["resources_started"] is True
        assert report["all_owned_resources_absent"] is True
    finally:
        if process.poll() is None:
            process.terminate()
            collect_probe(process, timeout=45)
        # Preserve failure evidence while recovering only this test's explicitly owned resources.
        for name in owned:
            inspected = subprocess.run(["docker", "container", "inspect", name], capture_output=True, text=True, timeout=5)
            if inspected.returncode == 0:
                actual = json.loads(inspected.stdout)[0]
                assert actual["Config"]["Labels"].get("coordinare.validation.issue") == "353"
                assert any(item["Source"].startswith(str(output)) for item in actual["Mounts"] if item["Destination"] == "/evidence")
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=True, timeout=15)

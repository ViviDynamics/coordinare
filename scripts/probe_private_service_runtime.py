"""Opt-in diagnostic runtime integration. Never produces bootstrap readiness."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ProbeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    guest_image: str
    guest_sha512: str = Field(pattern=r"^[0-9a-f]{128}$")
    architecture: str = Field(pattern=r"^(x86_64|aarch64)$")
    deadline_seconds: int = Field(gt=0, le=3600)
    cpus: int = Field(gt=0, le=8)
    memory_mib: int = Field(ge=512, le=16384)
    cache_bytes: int = Field(gt=0)
    scratch_bytes: int = Field(gt=0)
    reserve_bytes: int = Field(gt=0)
    scenario: Literal["full", "nested_kernel", "buildkit"] = "full"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_probe(config: ProbeConfiguration, output: Path, *, report: dict[str, Any]) -> dict[str, Any]:
    """Execute owned installer and consumer containers under explicit ceilings."""
    guest = Path(config.guest_image).resolve(strict=True)
    with guest.open("rb") as stream:
        if hashlib.file_digest(stream, "sha512").hexdigest() != config.guest_sha512:
            raise ValueError("guest_image_identity")
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("docker_unavailable")
    info = json.loads(subprocess.check_output(
        [docker, "info", "--format", "{{json .}}"], timeout=20,
    ))
    if config.cpus * 2 > info["NCPU"] or config.memory_mib * 2 * 1024**2 > info["MemTotal"]:
        raise ValueError("insufficient_runtime_capacity")
    if config.cache_bytes + config.scratch_bytes + config.reserve_bytes > shutil.disk_usage(output).free:
        raise ValueError("insufficient_storage_reserve")
    (output / "configuration.json").write_text(config.model_dump_json(indent=2) + "\n")
    cache = output / "cache"
    cache.mkdir(mode=0o700)
    (cache / "artifacts").mkdir()
    fixture = output / "fixture"
    shutil.copytree(Path(__file__).resolve().parents[1] / "tests/fixtures/private_service_runtime", fixture, symlinks=True)
    scripts = output / "probe-code"
    scripts.mkdir(mode=0o700)
    code_identity = {}
    for name in ("probe_private_service_runtime.py", "private_runtime_probe_guest.py", "private_runtime_probe_source.py"):
        target = scripts / name
        shutil.copyfile(Path(__file__).resolve().parent / name, target)
        code_identity[name] = _digest(target)
    (output / "code-identity.json").write_text(json.dumps(code_identity, indent=2) + "\n")
    deadline = time.monotonic() + config.deadline_seconds
    names: list[str] = []
    report["reason"] = "runtime_integration_incomplete"
    cleanup: list[bool] = []
    before = ""
    completed_all_cases = False

    def start_case(phase: str, label: str) -> tuple[str, subprocess.Popen[bytes], Path]:
        name = "coordinare-353-runtime-" + uuid.uuid4().hex[:12]
        names.append(name)
        (output / "ownership.json").write_text(json.dumps({"issue": 353, "containers": names,
                                                          "evidence_root": str(output.resolve())}, indent=2) + "\n")
        target = output / label
        target.mkdir(mode=0o700)
        memory = 512 if phase == "memory_pressure" else config.memory_mib
        args = [docker, "run", "--name", name, "--label", "coordinare.validation.issue=353", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--memory", str(memory) + "m", "--memory-swap", str(memory) + "m",
                "--cpus", str(config.cpus), "--pids-limit", "512",
                "--network", "bridge" if phase == "installer" else "none",
                "--mount", f"type=bind,src={guest},dst=/base/platform.qcow2,readonly",
                "--mount", f"type=bind,src={scripts},dst=/probe-code,readonly",
                "--mount", f"type=bind,src={fixture},dst=/fixture,readonly",
                "--mount", f"type=bind,src={output / 'configuration.json'},dst=/probe-config.json,readonly",
                "--mount", f"type=bind,src={target},dst=/evidence",
                "--mount", f"type=bind,src={cache},dst=/cache" + ("" if phase == "installer" else ",readonly"),
                "--entrypoint", "/usr/local/bin/python", config.image, "/probe-code/private_runtime_probe_guest.py", phase]
        report["resources_started"] = True
        with (target / "outer.log").open("wb") as log:
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        return name, process, target

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("diagnostic_deadline")
        if shutil.disk_usage(output).free < config.reserve_bytes:
            raise RuntimeError("storage_reserve_exhausted")
        allocated = sum(path.lstat().st_blocks * 512 for path in cache.rglob("*") if not path.is_symlink())
        if allocated > config.cache_bytes:
            raise RuntimeError("cache_allocation_exhausted")

    def finish_case(case: tuple[str, subprocess.Popen[bytes], Path]) -> tuple[int, dict[str, Any]]:
        name, process, target = case
        while process.poll() is None:
            check_deadline()
            time.sleep(0.5)
        path = target / "phase.json"
        evidence = json.loads(path.read_text()) if path.exists() else {"status": "held", "reason": "phase_evidence_missing"}
        actual = json.loads(subprocess.check_output([docker, "container", "inspect", name], timeout=20))[0]
        host = actual["HostConfig"]
        isolation = {"image": actual["Image"], "network": host["NetworkMode"], "privileged": host["Privileged"],
                     "cap_add": host["CapAdd"], "cap_drop": host["CapDrop"], "security_opt": host["SecurityOpt"],
                     "devices": host["Devices"], "ports": host["PortBindings"],
                     "memory": host["Memory"], "memory_swap": host["MemorySwap"], "nano_cpus": host["NanoCpus"],
                     "mounts": [{"destination": item["Destination"], "writable": item["RW"]} for item in actual["Mounts"]]}
        (target / "isolation.json").write_text(json.dumps(isolation, indent=2) + "\n")
        installer = target.name == "installer"
        cache_writable = next(item["RW"] for item in actual["Mounts"] if item["Destination"] == "/cache")
        safe = (actual["Image"] == config.image and not host["Privileged"] and not host["CapAdd"]
                and host["CapDrop"] == ["ALL"] and "no-new-privileges" in host["SecurityOpt"]
                and not host["Devices"] and not host["PortBindings"] and cache_writable == installer
                and host["NetworkMode"] == ("bridge" if installer else "none")
                and host["MemorySwap"] == host["Memory"] <= config.memory_mib * 1024**2
                and host["NanoCpus"] == config.cpus * 10**9)
        evidence["outer_isolation_verified"] = safe
        if not safe:
            raise RuntimeError("actual_outer_isolation_mismatch")
        return process.returncode, evidence

    def wait_for(case: tuple[str, subprocess.Popen[bytes], Path], filename: str) -> dict[str, Any]:
        _, process, target = case
        while process.poll() is None:
            check_deadline()
            path = target / filename
            if path.exists():
                try:
                    return json.loads(path.read_text())
                except json.JSONDecodeError:
                    pass
            time.sleep(0.2)
        raise RuntimeError("required_operation_not_reached")

    try:
        for phase in ("installer", "clean_consumer"):
            code, evidence = finish_case(start_case(phase, phase))
            report[phase] = evidence
            if code:
                report["reason"] = "runtime_" + phase + "_failed"
                return report
            if phase == "installer":
                before = _digest(cache / "installed.qcow2")
                report["installed_artifact_sha256_before"] = before
            else:
                report["installed_artifact_sha256_after"] = _digest(cache / "installed.qcow2")
                report["immutable_artifacts_unchanged"] = before == report["installed_artifact_sha256_after"]
        if "clean_consumer" in report and report["clean_consumer"].get("status") == "phase_completed":
            installer = report["installer"]
            consumer = report["clean_consumer"]
            report["source_identity_verified"] = bool(installer.get("source_identity_verified") and consumer.get("source_identity_verified"))
            report["egress_denial"] = consumer["egress_denial"]
            report["egress_denial"]["carrier_host"] = bool(
                installer["carrier_canary_exit_code"] == 0
                and installer["carrier_canary_response"] == "carrier-only\n"
                and consumer["canary_outer_response"] == "carrier-only\n"
                and consumer["carrier_canary_exit_code"] != 0,
            )
            report["egress_denial"]["proxy"] = bool(installer["proxy_canary_exit_code"] == 0
                and installer["proxy_canary_response"] == "carrier-only\n" and consumer["proxy_outer_response"] == "carrier-only\n"
                and consumer["proxy_canary_exit_code"] != 0)
            if not report["egress_denial"]["carrier_host"]:
                report["reason"] = "carrier_endpoint_isolation_not_proven"
                return report
            if not report["source_identity_verified"]:
                report["reason"] = "source_identity_mismatch"
                return report
            report["parent_export_denied"] = bool(installer.get("parent_export_denied") and consumer.get("parent_export_denied"))
            if config.scenario in {"nested_kernel", "buildkit"}:
                if config.scenario == "nested_kernel":
                    report["nested_kernel_execution_verified"] = all(
                        report[phase].get("nested_kernel_exit_code") == 0
                        and "NESTED_LINUX_WORKS" in report[phase].get("nested_kernel_output", "")
                        for phase in ("installer", "clean_consumer")
                    )
                else:
                    report["buildkit_cache_verified"] = all(report[phase].get("buildkit_cache_exit_code") == 0 for phase in ("installer", "clean_consumer"))
                completed_all_cases = True
            else:
                cancellations = {}
                for phase in ("boot", "build", "health", "stop"):
                    case = start_case("cancel_" + phase, "cancel_" + phase)
                    reached = wait_for(case, "cancel-ready.json")
                    if reached != {"phase": phase, "operation_active": True}:
                        raise RuntimeError("cancellation_point_invalid")
                    subprocess.run([docker, "kill", "--signal", "TERM", case[0]], check=True, capture_output=True, timeout=20)
                    code, evidence = finish_case(case)
                    cancellations[phase] = {"driver_exit_code": code, **evidence}
                    if not (code != 0 and evidence.get("cancelled") and evidence.get("qemu_reaped")
                            and evidence.get("control_keys_absent") and evidence.get("control_listener_absent")):
                        report["reason"] = "cancellation_cleanup_incomplete"
                        report["cancellation_cases"] = cancellations
                        return report
                report["cancellation_cases"] = cancellations
                report["cancellation_cleanup_confirmed"] = True
                concurrent = [start_case("concurrent", "concurrent_" + str(index)) for index in range(2)]
                live = [wait_for(case, "service-live.json") for case in concurrent]
                namespaces = [subprocess.check_output([docker, "inspect", "--format", "{{.NetworkSettings.SandboxKey}}", case[0]], text=True, timeout=20).strip() for case in concurrent]
                isolated = all(item == {"response": "source-fixture-v1\n"} for item in live) and len(set(namespaces)) == 2
                for case in concurrent:
                    (case[2] / "release").touch()
                completed = [finish_case(case) for case in concurrent]
                report["concurrent_port_isolation"] = isolated and all(code == 0 for code, _ in completed)
                report["concurrent_consumers"] = [evidence for _, evidence in completed]
                for phase in ("disk_pressure", "memory_pressure"):
                    code, evidence = finish_case(start_case(phase, phase))
                    report[phase] = {"driver_exit_code": code, **evidence}
                report["resource_exhaustion_cleanup_confirmed"] = bool(
                    report["disk_pressure"].get("disk_exhaustion_observed") and report["disk_pressure"]["driver_exit_code"] == 0
                    and report["memory_pressure"].get("memory_exhaustion_observed") and report["memory_pressure"]["driver_exit_code"] != 0
                    and all(report[phase].get("qemu_reaped") and report[phase].get("control_keys_absent") for phase in ("disk_pressure", "memory_pressure")),
                )
                report["installed_artifact_sha256_after"] = _digest(cache / "installed.qcow2")
                report["immutable_artifacts_unchanged"] = before == report["installed_artifact_sha256_after"]
                completed_all_cases = True
    except (OSError, RuntimeError, subprocess.SubprocessError):
        report["reason"] = "runtime_execution_failed"
    finally:
        cleanup_deadline = time.monotonic() + 60
        if names:
            try:
                subprocess.run([docker, "rm", "-f", *names], capture_output=True, timeout=30, check=False)
            except (OSError, subprocess.TimeoutExpired):
                report["reason"] = "runtime_cleanup_unconfirmed"
        for name in names:
            try:
                remaining = cleanup_deadline - time.monotonic()
                if remaining <= 0:
                    cleanup.append(False)
                    continue
                absent = subprocess.run([docker, "container", "inspect", name], capture_output=True, timeout=min(5, remaining), check=False)
                cleanup.append(absent.returncode != 0 and b"No such" in absent.stderr)
            except (OSError, subprocess.TimeoutExpired):
                cleanup.append(False)
        report["all_owned_resources_absent"] = all(cleanup)
        (output / "cleanup.json").write_text(json.dumps({"containers": names, "confirmed_absent": cleanup}, indent=2) + "\n")
    required = ("source_identity_verified", "parent_export_denied", "immutable_artifacts_unchanged", "all_owned_resources_absent")
    required += (("cancellation_cleanup_confirmed", "concurrent_port_isolation", "resource_exhaustion_cleanup_confirmed")
                 if config.scenario == "full" else ("nested_kernel_execution_verified",) if config.scenario == "nested_kernel" else ("buildkit_cache_verified",))
    ordinary = all(
        report.get(phase, {}).get("docker_response") == "source-fixture-v1\n"
        and report.get(phase, {}).get("compose_response") == "source-fixture-v1\n"
        and report.get(phase, {}).get("bind_mount_response") == "fresh-bind-source\n"
        and report.get(phase, {}).get("executable_source_response") == "source-fixture-v1\n"
        and report.get(phase, {}).get("host_key_mismatch_rejected") is True
        and report.get(phase, {}).get("guest_architecture") == config.architecture
        for phase in ("installer", "clean_consumer")
    )
    consumer = report.get("clean_consumer", {})
    negative = all(consumer.get(field) is not None and consumer[field] != 0 for field in (
        "missing_image_exit_code", "outer_cache_write_exit_code", "guest_cache_write_exit_code"))
    if (completed_all_cases and ordinary and negative and all(report.get(key) is True for key in required)
            and all(report.get("egress_denial", {}).get(key) is True for key in ("external", "carrier_host", "proxy", "metadata"))):
        report.update(status={"full": "fixture_passed", "nested_kernel": "nested_fixture_passed", "buildkit": "buildkit_fixture_passed"}[config.scenario],
                      reason="diagnostic_runtime_fixture_passed")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    def terminate(_signal: int, _frame: object) -> None:
        raise InterruptedError("owned_diagnostic_cancelled")

    signal.signal(signal.SIGTERM, terminate)
    try:
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        sys.stderr.write("Refusing to overwrite an existing diagnostic attempt\n")
        return 1
    report: dict[str, Any] = {"status": "held", "reason": "probe_configuration_invalid",
                              "resources_started": False, "readiness_evidence": False}
    try:
        config = ProbeConfiguration.model_validate(json.loads(args.config.read_text()))
        report = run_probe(config, args.output, report=report)
    except (ValidationError, ValueError, OSError, subprocess.SubprocessError):
        # Evidence/logs are private. Never echo unchecked config, credentials or exception text.
        pass
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    sys.stdout.write(json.dumps({"status": report["status"], "reason": report.get("reason")}) + "\n")
    return 0 if report["status"] in {"fixture_passed", "nested_fixture_passed", "buildkit_fixture_passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

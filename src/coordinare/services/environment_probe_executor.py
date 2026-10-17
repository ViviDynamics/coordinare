"""Run bounded capability observations with no writable source/cache or network."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.source_payload import materialize_sources

from coordinare.services import environment_probe_runtime
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_capabilities import _docker_identity
from coordinare.services.environment_probes import ProbeCommandResult, ProbePlan, ProbeReport

if TYPE_CHECKING:
    from collections.abc import Mapping


def probe_container_argv(plan: ProbePlan, source: Path, cache: Path) -> list[str]:
    platform = plan.capabilities.platform.replace("x86_64", "amd64").replace("aarch64", "arm64")
    return ["docker", "create", "--name", "coordinare-probe-" + plan.digest()[:24],
        "--platform", platform, "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=512m", "--cpus=1",
        "--tmpfs", "/scratch:rw,nosuid,nodev,size=64m,mode=1777", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m",
        "--mount", f"type=bind,source={source},target=/repo,readonly",
        "--mount", f"type=bind,source={cache},target=/cache,readonly",
        "--entrypoint", "/usr/local/bin/python", plan.capabilities.image_ref,
        "-I", "-c", "import time;time.sleep(180)"]


async def _docker(args: list[str], *, stdin: bytes | None = None, timeout: float = 15) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(stdin), timeout)
        return process.returncode or 0, stdout
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def _remove(name: str) -> bool:
    await _docker(["docker", "rm", "-f", "-v", name])
    code, found = await _docker(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"])
    # Confirm the daemon itself is reachable before treating inspect absence as proof.
    healthy, _ = await _docker(["docker", "info", "--format", "{{.ID}}"])
    return code == 0 and not found.strip() and healthy == 0


async def execute_environment_probe(
    plan: ProbePlan, *, state: Mapping[str, Any], source_files: Mapping[str, str], budget: AttemptBudget,
) -> ProbeReport:
    if state.get("probe_backend") != "docker":
        raise EnvironmentDiscoveryError("probe runtime cannot establish required isolation", code="probe_isolation_unsupported")
    actual = {path:hashlib.sha256(text.encode()).hexdigest() for path, text in source_files.items()}
    if actual != plan.inputs:
        raise EnvironmentDiscoveryError("probe source identity changed", code="probe_source_identity")
    budget.check()
    identity = await _docker_identity(plan.capabilities.image_ref)
    if identity != (plan.capabilities.image_ref, plan.capabilities.platform):
        raise EnvironmentDiscoveryError("probe image identity changed", code="probe_target_identity")
    name = "coordinare-probe-" + plan.digest()[:24]
    with tempfile.TemporaryDirectory(prefix="coordinare-probe-") as temporary:
        root = Path(temporary)
        materialize_sources(source_files, root/"repo")
        (root/"cache").mkdir()
        create = asyncio.create_task(_docker(probe_container_argv(plan, root/"repo", root/"cache")))
        try:
            code, _ = await asyncio.shield(create)
            if code:
                raise EnvironmentDiscoveryError("probe container could not be created", code="probe_executor_failed")
            code, _ = await _docker(["docker", "start", name])
            if code:
                raise EnvironmentDiscoveryError("probe container could not start", code="probe_executor_failed")
            seconds = min(120, max(0, budget.deadline - time.monotonic()))
            payload = json.dumps({"commands":[command.model_dump(mode="json") for command in plan.request.commands],
                "seconds":seconds}).encode()
            runtime = Path(str(environment_probe_runtime.__file__)).read_text()
            code, output = await _docker(["docker", "exec", "-i", name, "/usr/local/bin/python", "-I", "-c", runtime],
                stdin=payload, timeout=seconds+1)
            if code or len(output) > 300000:
                raise EnvironmentDiscoveryError("probe runtime did not return bounded evidence", code="probe_executor_failed")
            results = [ProbeCommandResult.model_validate(item) for item in json.loads(output)]
        finally:
            # A cancelled create may still finish: drain it before removing its known name.
            await asyncio.shield(create)
            cleanup = asyncio.create_task(_remove(name))
            if not await asyncio.shield(cleanup):
                raise EnvironmentDiscoveryError("probe executor cleanup is unconfirmed", code="cleanup_unconfirmed")
    return ProbeReport(schema_version=1, plan_digest=plan.digest(), source_commit=plan.source_commit,
        image_ref=plan.capabilities.image_ref, platform=plan.capabilities.platform, results=results, cleanup_confirmed=True)

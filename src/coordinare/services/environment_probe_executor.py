"""Run bounded capability observations with no writable source/cache or network."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from coordinare_service_inference.environment.execution_source import (
    source_refusal,
    validate_execution_bundle,
)
from coordinare_service_inference.environment.execution_source_restore import (
    restore_execution_bundle,
)

from coordinare.lib.redaction import redact_command_text
from coordinare.services import environment_probe_runtime
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_capabilities import _docker_identity
from coordinare.services.environment_probes import (
    ProbeCommandResult,
    ProbePlan,
    ProbeReport,
    probe_batch_timeout,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coordinare_service_inference.environment.execution_source import ExecutionSourceBundle


log = structlog.get_logger(__name__)
MAX_WIRE_REPORT_BYTES = 300000
STDERR_TAIL_BYTES = 400
_ACTIVE_PROBES: set[str] = set()
RUNTIME_SOURCE = Path(str(environment_probe_runtime.__file__)).read_text()


def probe_container_argv(plan: ProbePlan, source: Path, cache: Path, *, scratch: Path | None = None) -> list[str]:
    platform = plan.capabilities.platform.replace("x86_64", "amd64").replace("aarch64", "arm64")
    resources = ['--pids-limit=128', '--memory=512m', '--cpus=1',
        '--tmpfs', '/scratch:rw,nosuid,nodev,size=64m,mode=1777']
    if plan.request.execution_context == 'private_service':
        policy = plan.capabilities.runtime
        if policy is None or scratch is None:
            message = 'private preflight requires runtime policy and private scratch'
            raise ValueError(message)
        limits = policy.resources
        resources = ['--pids-limit=512', f'--memory={limits.memory_mib}m',
            f'--memory-swap={limits.memory_mib}m', f'--cpus={limits.cpus}',
            '--mount', f'type=bind,source={scratch},target=/scratch']
    return ["docker", "create", "--name", "coordinare-probe-" + plan.digest()[:24],
        "--platform", platform, "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", *resources,
        "--tmpfs", str(Path("/") / "tmp") + ":rw,nosuid,nodev,size=16m",
        "--mount", f"type=bind,source={source},target=/repo,readonly",
        "--mount", f"type=bind,source={cache},target=/cache,readonly",
        "--entrypoint", "/usr/local/bin/python", plan.capabilities.image_ref,
        "-I", "-c", "import time;time.sleep(180)"]


async def _collect(process: asyncio.subprocess.Process, stdin: bytes | None) -> bytes:
    if stdin is not None and process.stdin is not None:
        process.stdin.write(stdin)
        await process.stdin.drain()
        process.stdin.close()
    output = bytearray()
    if process.stdout is not None:
        while chunk := await process.stdout.read(8192):
            if len(output) + len(chunk) > MAX_WIRE_REPORT_BYTES:
                message = "probe transport output exceeded its byte bound"
                raise EnvironmentDiscoveryError(message, code="probe_output_limit")
            output.extend(chunk)
    await process.wait()
    return bytes(output)


async def _docker(args: list[str], *, stdin: bytes | None = None, deadline_seconds: float = 15,
                  stderr: Any = asyncio.subprocess.DEVNULL) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=stderr)
    try:
        stdout = await asyncio.wait_for(_collect(process, stdin), deadline_seconds)
        return process.returncode or 0, stdout
    finally:
        if process.returncode is None:
            process.kill()
        # A paused full pipe can delay asyncio's process.wait even after kill.
        # Discard buffered bytes under a bound; never collect an oversized report.
        async with asyncio.timeout(2):
            if process.stdout is not None:
                while await process.stdout.read(8192):
                    pass
            await process.wait()


async def _remove(name: str) -> bool:
    await _docker(["docker", "rm", "-f", "-v", name])
    code, found = await _docker(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"])
    # Confirm the daemon itself is reachable before treating inspect absence as proof.
    healthy, _ = await _docker(["docker", "info", "--format", "{{.ID}}"])
    return code == 0 and not found.strip() and healthy == 0


async def _settle(task: asyncio.Task[Any]) -> tuple[Any, bool]:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        return task.result(), cancelled
    except (Exception, asyncio.CancelledError):
        return None, cancelled


async def _bounded_remove(name: str) -> bool:
    async with asyncio.timeout(30):
        return await _remove(name)


async def confirm_probe_cleanup(plan: ProbePlan) -> bool:
    name = "coordinare-probe-" + plan.digest()[:24]
    return False if name in _ACTIVE_PROBES else await _bounded_remove(name)


def _executor_failed(step: str, code: int, command: Mapping[str, Any], *, output_bytes: int | None = None,
                     stderr_tail: str | None = None) -> None:
    """Which step failed and how. Only the in-container runtime's own error tail is kept, bounded and redacted;
    Docker's host-side messages can carry host paths and are not."""
    log.warning("environment_probe.executor_failed", step=step, exit_code=code, command_id=command.get("id"),
                output_bytes=output_bytes,
                runtime_error=redact_command_text(stderr_tail)[-STDERR_TAIL_BYTES:] if stderr_tail else None)


async def _command_container(plan: ProbePlan, roots: tuple[Path, Path], command: dict[str, Any], deadline: float,
                             *, scratch: Path | None = None) -> ProbeCommandResult:
    name = "coordinare-probe-" + plan.digest()[:24]
    create = asyncio.create_task(_docker(probe_container_argv(plan, *roots, scratch=scratch), deadline_seconds=max(0.01, min(15, deadline-time.monotonic()))))
    try:
        code, _ = await asyncio.shield(create)
        if code:
            _executor_failed("create", code, command)
            message = "probe container could not be created"
            raise EnvironmentDiscoveryError(message, code="probe_executor_failed")
        code, _ = await _docker(["docker", "start", name], deadline_seconds=max(0.01, deadline-time.monotonic()))
        if code:
            _executor_failed("start", code, command)
            message = "probe container could not start"
            raise EnvironmentDiscoveryError(message, code="probe_executor_failed")
        seconds = max(0, deadline-time.monotonic())
        data: dict[str, Any] = {"commands":[command], "seconds":seconds}
        if plan.request.execution_context == 'private_service':
            if plan.capabilities.runtime is None:
                message = 'private preflight runtime is missing'
                raise ValueError(message)
            data.update(runtime=plan.capabilities.runtime.model_dump(mode='json'),
                        execution_source_digest=plan.execution_source_digest,
                        expires_at=time.time()+max(0, deadline-time.monotonic()))
        payload = json.dumps(data).encode()
        with tempfile.TemporaryFile() as errors:
            code, output = await _docker(["docker", "exec", "-i", name, "/usr/local/bin/python", "-I", "-c", RUNTIME_SOURCE],
                stdin=payload, deadline_seconds=seconds+1, stderr=errors)
            errors.seek(max(0, errors.seek(0, 2) - STDERR_TAIL_BYTES))
            tail = errors.read().decode(errors='ignore')
        if code or len(output) > MAX_WIRE_REPORT_BYTES:
            _executor_failed("runtime", code, command, output_bytes=len(output), stderr_tail=tail)
            message = "probe runtime did not return bounded evidence"
            raise EnvironmentDiscoveryError(message, code="probe_executor_failed")
        return ProbeCommandResult.model_validate(json.loads(output)[0])
    finally:
        # Even a failed create can have been accepted by Docker. Never let its
        # exception prevent cleanup, and drain cleanup despite further cancellation.
        _, cancelled = await _settle(create)
        cleanup = asyncio.create_task(_bounded_remove(name))
        confirmed, cleanup_cancelled = await _settle(cleanup)
        if confirmed is not True:
            message = "probe executor cleanup is unconfirmed"
            raise EnvironmentDiscoveryError(message, code="cleanup_unconfirmed")
        if cancelled or cleanup_cancelled:
            raise asyncio.CancelledError


async def execute_environment_probe(
    plan: ProbePlan, *, state: Mapping[str, Any], source_bundle: ExecutionSourceBundle, budget: AttemptBudget, deadline: float | None = None,
) -> ProbeReport:
    if state.get("probe_backend") != "docker":
        message = "probe runtime cannot establish required isolation"
        raise EnvironmentDiscoveryError(message, code="probe_isolation_unsupported")
    try:
        validate_execution_bundle(source_bundle.payload_path, expected=source_bundle.identity, review_inputs=plan.inputs)
        if plan.execution_source_digest is not None and plan.execution_source_digest != source_bundle.identity.digest():
            source_refusal('probe complete execution source differs')
        if source_bundle.identity.source_commit != plan.source_commit:
            source_refusal('execution source commit differs')
    except ValueError as exc:
        message = 'probe source identity changed'
        raise EnvironmentDiscoveryError(message, code='probe_source_identity') from exc
    budget.check()
    name = "coordinare-probe-" + plan.digest()[:24]
    if name in _ACTIVE_PROBES:
        message = "probe executor already owns this resource"
        raise EnvironmentDiscoveryError(message, code="probe_reservation_invalid")
    deadline = min(budget.deadline, time.monotonic()+probe_batch_timeout(plan),
                   deadline if deadline is not None else budget.deadline)
    _ACTIVE_PROBES.add(name)
    try:
        async with asyncio.timeout_at(deadline):
            identity = await _docker_identity(plan.capabilities.image_ref)
            if identity != (plan.capabilities.image_ref, plan.capabilities.platform):
                message = "probe image identity changed"
                raise EnvironmentDiscoveryError(message, code="probe_target_identity")
            with tempfile.TemporaryDirectory(prefix="coordinare-probe-") as temporary:
                root = Path(temporary)
                restore_execution_bundle(source_bundle.payload_path, root/"repo",
                    expected=source_bundle.identity, review_inputs=plan.inputs)
                (root/"cache").mkdir()
                # Independent commands get separate containers: removal kills even
                # escaped sessions before another command can run.
                results = []
                for command in plan.request.commands:
                    if plan.request.execution_context == 'private_service':
                        with tempfile.TemporaryDirectory(prefix='runtime-scratch-', dir=root) as scratch:
                            results.append(await _command_container(plan, (root/'repo', root/'cache'),
                                command.model_dump(mode='json'), deadline, scratch=Path(scratch)))
                    else:
                        results.append(await _command_container(plan, (root/'repo', root/'cache'),
                            command.model_dump(mode='json'), deadline))
    finally:
        _ACTIVE_PROBES.discard(name)
    return ProbeReport(execution_source_digest=plan.execution_source_digest, schema_version=1, plan_digest=plan.digest(), source_commit=plan.source_commit,
        image_ref=plan.capabilities.image_ref, platform=plan.capabilities.platform, results=results, cleanup_confirmed=True)

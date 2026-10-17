"""Execute accepted argv declarations; child output never controls trusted outcomes."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import time
from graphlib import TopologicalSorter
from pathlib import Path
from typing import TYPE_CHECKING

from .models import CheckResult, Command, EnvironmentManifest, VerificationReport, environment_order

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

OUTPUT_LIMIT = 4096
ATTEMPT_OUTPUT_LIMIT = 256 * 1024
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str, roots: dict[str, str], env: dict[str, str]) -> str:
    for key, root in roots.items():
        value = value.replace("{" + key + "}", root)

    def reference(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise ValueError(f"unresolved environment reference: {name}")
        return env[name]

    return _ENV_REFERENCE.sub(reference, value)


def manifest_environment(
    manifest: EnvironmentManifest,
    *,
    cache: str | Path,
    repo: str | Path,
    scratch: str | Path,
) -> dict[str, str]:
    """Apply only declared activation, without executing cache scripts or starting services."""
    roots = {"cache": str(cache), "repo": str(repo), "scratch": str(scratch)}
    env = dict(os.environ)
    # Bash startup hooks must never run before an explicit declared command.
    env["BASH_ENV"] = ""
    env["ENV"] = ""
    for key in environment_order(manifest.activation.env):
        env[key] = _expand(manifest.activation.env[key], roots, env)
    paths = [_expand(path, roots, env) for path in manifest.activation.path]
    env["PATH"] = os.pathsep.join(dict.fromkeys([*paths, *env.get("PATH", "").split(os.pathsep)]))
    return env


def _redact(text: str, env: dict[str, str]) -> str:
    values = {
        value
        for key, value in env.items()
        if value and re.search(r"token|secret|password|credential|api.?key", key, re.IGNORECASE)
    }
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


async def _terminate(process: asyncio.subprocess.Process) -> None:
    # A successful parent can still leave descendants behind in its process group.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        await asyncio.wait_for(process.wait(), timeout=5)


async def _execute(
    command: Command,
    *,
    roots: dict[str, str],
    env: dict[str, str],
    digest: str,
    scope: str,
    results: dict[str, CheckResult],
    background: bool = False,
    processes: list[asyncio.subprocess.Process] | None = None,
    drains: list[asyncio.Task[None]] | None = None,
    ignore_dependencies: bool = False,
) -> CheckResult:
    result = CheckResult(
        id=command.id,
        status="not_run",
        required=command.required,
        manifest_digest=digest,
        context=scope,
    )
    if not ignore_dependencies and any(
        key not in results or results[key].status != "pass" for key in command.depends_on
    ):
        result.output = "prerequisite did not pass"
        return result
    started = time.monotonic()
    process = None
    drain = None
    tail = bytearray()
    total = 0
    child_env = dict(env)
    retained_limit = OUTPUT_LIMIT * 2 + max(
        (
            len(value.encode())
            for key, value in env.items()
            if re.search(r"token|secret|password|credential|api.?key", key, re.IGNORECASE)
        ),
        default=0,
    )
    try:
        for key in environment_order(command.env):
            child_env[key] = _expand(command.env[key], roots, child_env)
        argv = [_expand(arg, roots, child_env) for arg in command.argv]
        cwd = _expand(command.cwd, roots, child_env)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

        async def capture() -> None:
            nonlocal total
            assert process is not None and process.stdout is not None
            while chunk := await process.stdout.read(8192):
                total += len(chunk)
                tail.extend(chunk)
                if len(tail) > retained_limit:
                    del tail[:-retained_limit]

        drain = asyncio.create_task(capture())
        if background:
            await asyncio.sleep(min(0.1, command.timeout_seconds))
            if process.returncode is None:
                assert processes is not None and drains is not None
                processes.append(process)
                drains.append(drain)
                result.status = "pass"
                return result
        try:
            await asyncio.wait_for(process.wait(), command.timeout_seconds)
            result.exit_code = process.returncode
            result.status = "pass" if process.returncode == 0 else "fail"
        except TimeoutError:
            result.status = "indeterminate"
            tail.extend(b"\ncommand timed out")
    except (OSError, ValueError) as exc:
        result.status = "fail"
        tail.extend(str(exc).encode())
    finally:
        if process is not None and (processes is None or process not in processes):
            await _terminate(process)
            if drain is not None:
                await drain
        result.duration_seconds = time.monotonic() - started
        cleaned = _redact(tail.decode(errors="replace"), child_env).encode()
        result.output_truncated = total > OUTPUT_LIMIT or len(cleaned) > OUTPUT_LIMIT
        result.output = cleaned[-OUTPUT_LIMIT:].decode(errors="ignore")
    return result


async def _commands(
    commands: list[Command],
    *,
    roots: dict[str, str],
    env: dict[str, str],
    digest: str,
    scope: str,
    results: dict[str, CheckResult],
) -> None:
    pending = {command.id: command for command in commands}
    while pending:
        ready = [
            command
            for command in pending.values()
            if not any(dep in pending for dep in command.depends_on)
        ]
        if not ready:
            raise ValueError("unresolvable command ordering")
        for command in ready:
            if command.scope and scope not in command.scope:
                results[command.id] = CheckResult(
                    id=command.id,
                    status="not_run",
                    required=False,
                    output="outside requested scope",
                    manifest_digest=digest,
                    context=scope,
                )
            else:
                results[command.id] = await _execute(
                    command,
                    roots=roots,
                    env=env,
                    digest=digest,
                    scope=scope,
                    results=results,
                )
            del pending[command.id]


@contextlib.asynccontextmanager
async def manifest_services(
    manifest: EnvironmentManifest,
    *,
    cache: str | Path,
    repo: str | Path,
    scratch: str | Path,
    _results: dict[str, CheckResult] | None = None,
    scope: str = "",
) -> AsyncIterator[list[CheckResult]]:
    """Hold explicitly declared services until exit; expose lifecycle failures to callers."""
    roots = {"cache": str(cache), "repo": str(repo), "scratch": str(scratch)}
    env = manifest_environment(manifest, **roots)
    results = _results if _results is not None else {}
    lifecycle: list[CheckResult] = []
    started = []
    processes: list[asyncio.subprocess.Process] = []
    drains: list[asyncio.Task[None]] = []
    try:
        command_service = {command.id: service.id for service in manifest.services
                           for command in [service.init, service.start, service.health] if command}
        by_id = {service.id: service for service in manifest.services}
        graph = {service.id: {command_service[dependency]
                             for command in [service.init, service.start, service.health] if command
                             for dependency in command.depends_on
                             if dependency in command_service and command_service[dependency] != service.id}
                 for service in manifest.services}
        for service_id in TopologicalSorter(graph).static_order():
            service = by_id[service_id]
            if service.start.scope and scope not in service.start.scope:
                continue
            started.append(service)
            healthy = True
            for command in [service.init, service.start, service.health]:
                if command is None:
                    continue
                if healthy:
                    result = await _execute(
                        command,
                        roots=roots,
                        env=env,
                        digest=manifest.digest(),
                        scope=scope,
                        results=results,
                        background=command is service.start,
                        processes=processes,
                        drains=drains,
                    )
                else:
                    result = CheckResult(
                        id=command.id,
                        status="not_run",
                        required=service.required,
                        manifest_digest=manifest.digest(),
                        context=scope,
                        output="service prerequisite did not pass",
                    )
                result.required = service.required
                results[result.id] = result
                lifecycle.append(result)
                healthy = healthy and result.status == "pass"
        yield lifecycle
    finally:
        try:
            async with asyncio.timeout(30):
                for service in reversed(started):
                    result = await _execute(
                        service.stop,
                        roots=roots,
                        env=env,
                        digest=manifest.digest(),
                        scope=scope,
                        results=results,
                        ignore_dependencies=True,
                    )
                    result.required = service.required
                    results[result.id] = result
                    lifecycle.append(result)
        finally:
            for process in processes:
                await _terminate(process)
            if drains:
                await asyncio.gather(*drains)


async def run_manifest(
    manifest: EnvironmentManifest,
    *,
    cache: str | Path,
    repo: str | Path,
    scratch: str | Path,
    install: bool = False,
    scope: str = "",
) -> VerificationReport:
    roots = {"cache": str(cache), "repo": str(repo), "scratch": str(scratch)}
    env = manifest_environment(manifest, **roots)
    results: dict[str, CheckResult] = {}
    digest = manifest.digest()
    if install:
        await _commands(
            manifest.install, roots=roots, env=env, digest=digest, scope=scope, results=results,
        )
    # Activation paths are execution prerequisites, even when a command uses absolute argv.
    missing = [
        path for path in manifest.activation.path if not Path(_expand(path, roots, env)).is_dir()
    ]
    if missing:
        results["activation"] = CheckResult(
            id="activation",
            status="fail",
            required=True,
            output="declared activation path missing",
            manifest_digest=digest,
            context=scope,
        )
    async with manifest_services(manifest, **roots, _results=results, scope=scope) as services:
        if missing or any(
            r.required and r.status != "pass" for r in [*results.values(), *services]
        ):
            for check in manifest.checks:
                results[check.id] = CheckResult(
                    id=check.id,
                    status="not_run",
                    required=check.required,
                    output="environment prerequisite failed",
                    manifest_digest=digest,
                    context=scope,
                )
        else:
            await _commands(
                manifest.checks, roots=roots, env=env, digest=digest, scope=scope, results=results,
            )
    if not any(
        check.required and (not check.scope or scope in check.scope) for check in manifest.checks
    ):
        results["applicable_checks"] = CheckResult(
            id="applicable_checks",
            status="not_run",
            required=True,
            output="no required baseline check applies to requested scope",
            manifest_digest=digest,
            context=scope,
        )
    budget = ATTEMPT_OUTPUT_LIMIT
    for result in results.values():
        output = result.output.encode()
        if len(output) > budget:
            result.output = output[:budget].decode(errors="ignore")
            result.output_truncated = True
        budget -= len(result.output.encode())
    order = {
        command.id: index for index, command in enumerate([*manifest.install, *manifest.checks])
    }
    return VerificationReport(
        manifest_digest=digest,
        scope=scope,
        results=sorted(results.values(), key=lambda r: order.get(r.id, -1)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest")
    parser.add_argument("--manifest-stdin", action="store_true")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--scope", default="")
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    manifest = EnvironmentManifest.model_validate(
        json.loads(sys.stdin.read() if args.manifest_stdin else args.manifest),
    )
    report = asyncio.run(
        run_manifest(
            manifest,
            cache=args.cache,
            repo=args.repo,
            scratch=args.scratch,
            scope=args.scope,
            install=args.install,
        ),
    )
    sys.stdout.write(report.model_dump_json() + "\n")
    raise SystemExit(0 if report.passed else 1)


if __name__ == "__main__":
    main()

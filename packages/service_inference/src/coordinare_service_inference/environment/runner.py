"""Execute accepted argv declarations; child output never controls trusted outcomes."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
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
    from collections.abc import AsyncIterator, Sequence

OUTPUT_LIMIT = 4096
ATTEMPT_OUTPUT_LIMIT = 256 * 1024
_VALUE_REFERENCE = re.compile(r"\$\{(?P<environment>[A-Za-z_][A-Za-z0-9_]*)\}|\{(?P<root>cache|repo|scratch)\}")


# This isolated helper owns only one command's descendant tree. Its environment is
# delivered after trusted Python starts, so loader variables cannot preempt supervision.
_LINUX_SUPERVISOR = r"""
from __future__ import annotations
import contextlib
import ctypes
import json
import os
import signal
import sys
import time

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
    raise SystemExit(125)
stopping = False
def terminate(signum, frame):
    global stopping
    stopping = True
for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(signum, terminate)
parent = os.getppid()
if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
    raise SystemExit(125)
if os.getppid() != parent:
    stopping = True
with os.fdopen(int(sys.argv[1]), 'r') as stream:
    environment = json.load(stream)
service = sys.argv[2] == 'service'
ack_fd = int(sys.argv[3])
control_fd = int(sys.argv[4])
if control_fd >= 0:
    os.set_blocking(control_fd, False)
stop_requested = False
def poll_control():
    global stop_requested, status
    if control_fd < 0 or stop_requested:
        return
    try:
        requested = os.read(control_fd, 1)
    except BlockingIOError:
        return
    if requested == b'1':
        # Capture a pre-existing failure before authorizing signal termination.
        if status is None:
            found, value = os.waitpid(child, os.WNOHANG)
            if found:
                status = value
        stop_requested = status is None or os.waitstatus_to_exitcode(status) == 0
        os.write(ack_fd, b'1')
argv = sys.argv[5:]
child = os.fork()
if child == 0:
    if ack_fd >= 0:
        os.close(ack_fd)
        os.close(control_fd)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, signal.SIG_DFL)
    try:
        os.execvpe(argv[0], argv, environment)
    except OSError:
        os._exit(127)
status = None
while status is None and not stopping:
    poll_control()
    if status is not None:
        break
    found, value = os.waitpid(child, os.WNOHANG)
    if found:
        status = value
    else:
        time.sleep(0.01)
if status is None:
    # Preserve a service failure that occurred before teardown was requested.
    found, value = os.waitpid(child, os.WNOHANG)
    if found:
        status = value
exit_code = os.waitstatus_to_exitcode(status) if status is not None else (
    0 if service and stopping and stop_requested else 143
)
if service and stop_requested and exit_code == -signal.SIGTERM:
    exit_code = 0
if exit_code < 0:
    exit_code = 128 - exit_code
children_path = '/proc/self/task/' + str(os.getpid()) + '/children'
def children():
    with open(children_path) as stream:
        return [int(value) for value in stream.read().split()]
def reap():
    while True:
        try:
            found, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not found:
            return
if service and exit_code == 0:
    while not stopping:
        poll_control()
        reap()
        if not children():
            break
        time.sleep(0.01)
deadline = time.monotonic() + 5
while True:
    for descendant in children():
        with contextlib.suppress(ProcessLookupError):
            os.kill(descendant, signal.SIGKILL)
    reap()
    if not children():
        break
    if time.monotonic() >= deadline:
        exit_code = 125  # Never report success without confirmed descendant cleanup.
        break
    time.sleep(0.01)
# All results and descendant cleanup are final. Avoid CPython finalization resetting
# signal handlers while the parent can still deliver its teardown signal.
os._exit(exit_code)
"""


def _expand(value: str, roots: dict[str, str], env: dict[str, str]) -> str:
    def reference(match: re.Match[str]) -> str:
        name = match.group("environment")
        if name is None:
            return roots[match.group("root")]
        if name not in env:
            raise ValueError(f"unresolved environment reference: {name}")
        return env[name]

    return _VALUE_REFERENCE.sub(reference, value)


def manifest_environment(
    manifest: EnvironmentManifest,
    *,
    cache: str | Path,
    repo: str | Path,
    scratch: str | Path,
    secret_names: Sequence[str] = (),
) -> dict[str, str]:
    """Apply only declared activation, without executing cache scripts or starting services."""
    roots = {"cache": str(cache), "repo": str(repo), "scratch": str(scratch)}
    env = dict(os.environ)
    try:
        existing_names = json.loads(env.get('COORDINARE_ENV_SECRET_NAMES', '[]'))
    except ValueError:
        existing_names = []
    inherited = {name for name in existing_names if isinstance(name, str)} if isinstance(existing_names, list) else set()
    env['COORDINARE_ENV_SECRET_NAMES'] = json.dumps(sorted(inherited | set(secret_names)))
    # Bash startup hooks must never run before an explicit declared command.
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    for key in environment_order(manifest.activation.env):
        env[key] = _expand(manifest.activation.env[key], roots, env)
    paths = [_expand(path, roots, env) for path in manifest.activation.path]
    env["PATH"] = os.pathsep.join(dict.fromkeys(
        path for path in [*paths, *env.get("PATH", "").split(os.pathsep)] if path
    ))
    return env


def _secret_values(env: dict[str, str]) -> set[str]:
    try:
        declared = json.loads(env.get("COORDINARE_ENV_SECRET_NAMES", "[]"))
    except ValueError:
        declared = []
    names = set(declared) if isinstance(declared, list) and all(isinstance(n, str) for n in declared) else set()
    return {value for key, value in env.items() if key != "COORDINARE_ENV_SECRET_NAMES" and value and (
        key in names or re.search(r"token|secret|password|credential|api.?key", key, re.IGNORECASE)
        or ("://" in value and re.search(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]+:[^\s/@]+@", value))
    )}


def _runtime_secret_values(secret_names: Sequence[str]) -> frozenset[str]:
    original = dict(os.environ)
    return frozenset(_secret_values(original) | {
        original[name] for name in secret_names if original.get(name)
    })


def _redact(
    text: str, env: dict[str, str], *, extra_values: frozenset[str] = frozenset(),
) -> str:
    for value in sorted(_secret_values(env) | extra_values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    if "://" not in text:
        return text
    return re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if sys.platform == 'linux' and process.returncode is None:
        # Give the command-local subreaper a chance to reap detached descendants.
        with contextlib.suppress(ProcessLookupError):
            # Popen.send_signal() polls/reaps an already-exited child, racing
            # asyncio's pidfd watcher and replacing its real exit status with 255.
            os.kill(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=6)
            return
        except TimeoutError:
            pass
    # A successful parent can still leave descendants behind in its process group.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        # Process.wait() can also wait for inherited stdout to reach EOF. Keep
        # that wait bounded; the caller drains or closes the pipe separately.
        with contextlib.suppress(ProcessLookupError, TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=0.25)


async def _notify_service_stop(controls: tuple[int, int]) -> bool:
    try:
        control_write, ack_read = controls
        os.write(control_write, b'1')
        deadline = time.monotonic() + 1
        while True:
            try:
                return os.read(ack_read, 1) == b'1'
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(0.01)
    except OSError:
        return False


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
    service_controls: dict[int, tuple[int, int]] | None = None,
    ignore_dependencies: bool = False,
    redactions: frozenset[str] = frozenset(),
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
    ack_read = ack_write = None
    control_read = control_write = None
    drain = None
    tail = bytearray()
    total = 0
    child_env = dict(env)
    known_secrets = frozenset(_secret_values(env)) | redactions
    retained_limit = OUTPUT_LIMIT * 2 + max((len(value.encode()) for value in known_secrets), default=0)
    try:
        for key in environment_order(command.env):
            child_env[key] = _expand(command.env[key], roots, child_env)
        retained_limit = max(retained_limit, OUTPUT_LIMIT * 2 + max(
            (len(value.encode()) for value in _secret_values(child_env)), default=0,
        ))
        argv = [_expand(arg, roots, child_env) for arg in command.argv]
        cwd = _expand(command.cwd, roots, child_env)
        arguments = argv
        child_policy = child_env
        inherited_fds: tuple[int, ...] = ()
        read_fd = write_fd = None
        if sys.platform == 'linux':
            read_fd, write_fd = os.pipe()
            inherited_fds = (read_fd,)
            if background:
                ack_read, ack_write = os.pipe()
                os.set_blocking(ack_read, False)
                control_read, control_write = os.pipe()
                inherited_fds += (ack_write, control_read)
            arguments = [sys.executable, '-I', '-c', _LINUX_SUPERVISOR, str(read_fd),
                         'service' if background else 'command', str(ack_write if ack_write is not None else -1),
                         str(control_read if control_read is not None else -1), *argv]
            child_policy = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments, cwd=cwd, env=child_policy,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                pass_fds=inherited_fds,
            )
            if ack_write is not None:
                os.close(ack_write)
                ack_write = None
            if control_read is not None:
                os.close(control_read)
                control_read = None
            if read_fd is not None:
                os.close(read_fd)
                read_fd = None
            if write_fd is not None:
                payload = json.dumps(child_env).encode()
                target_fd = write_fd
                write_fd = None

                def supply_environment() -> None:
                    with os.fdopen(target_fd, 'wb') as stream:
                        stream.write(payload)

                await asyncio.to_thread(supply_environment)
        finally:
            for descriptor in (read_fd, write_fd):
                if descriptor is not None:
                    os.close(descriptor)


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
                if ack_read is not None:
                    assert service_controls is not None
                    assert control_write is not None
                    service_controls[process.pid] = (control_write, ack_read)
                    ack_read = None
                    control_write = None
                if sys.platform == 'linux':
                    result.status = "pass"
                else:
                    # Process-group cleanup cannot prove a running service's
                    # successful completion; only the Linux supervisor can.
                    result.status = "indeterminate"
                    tail.extend(b"\nbackground service completion requires Linux supervision")
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
        for descriptor in (ack_read, ack_write, control_read, control_write):
            if descriptor is not None:
                os.close(descriptor)
        if process is not None and (processes is None or process not in processes):
            await _terminate(process)
            if drain is not None:
                try:
                    await asyncio.wait_for(drain, timeout=0.25)
                except TimeoutError:
                    result.status = 'indeterminate'
                    tail.extend(b'\noutput pipe remained open after process-group cleanup')
                    # asyncio exposes no public Process.close(); close its transport to
                    # release pipes held by detached descendants before container teardown.
                    transport = getattr(process, '_transport', None)
                    if transport is not None:
                        transport.close()
        if sys.platform == 'linux' and process is not None and (
            process.returncode is not None and (process.returncode < 0 or process.returncode == 125)
        ):
            cleanup_id = '__infrastructure__/cleanup/' + command.id
            results[cleanup_id] = CheckResult(
                id=cleanup_id, status='indeterminate', required=True,
                manifest_digest=digest, context=scope,
                output='command supervisor did not confirm descendant cleanup',
            )
        result.duration_seconds = time.monotonic() - started
        cleaned = _redact(tail.decode(errors="replace"), child_env, extra_values=known_secrets).encode()
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
    redactions: frozenset[str] = frozenset(),
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
            if scope and command.scope and scope not in command.scope:
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
                    redactions=redactions,
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
    secret_names: Sequence[str] = (),
) -> AsyncIterator[list[CheckResult]]:
    """Hold explicitly declared services until exit; expose lifecycle failures to callers."""
    roots = {"cache": str(cache), "repo": str(repo), "scratch": str(scratch)}
    redactions = _runtime_secret_values(secret_names)
    env = manifest_environment(manifest, **roots, secret_names=secret_names)
    results = _results if _results is not None else {}
    lifecycle: list[CheckResult] = []
    started = []
    processes: list[asyncio.subprocess.Process] = []
    drains: list[asyncio.Task[None]] = []
    supervisors: dict[int, str] = {}
    service_controls: dict[int, tuple[int, int]] = {}
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
            if scope and service.start.scope and scope not in service.start.scope:
                continue
            started.append(service)
            healthy = True
            for command in [service.init, service.start, service.health]:
                if command is None:
                    continue
                if healthy:
                    previous_processes = len(processes)
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
                        service_controls=service_controls,
                        redactions=redactions,
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
                if healthy and len(processes) > previous_processes:
                    supervisors[processes[-1].pid] = command.id
                result.required = service.required
                results[result.id] = result
                lifecycle.append(result)
                healthy = healthy and result.status == "pass"
        yield lifecycle
    finally:
        try:
            async with asyncio.timeout(30):
                for service in reversed(started):
                    for process in processes:
                        if supervisors[process.pid] != service.start.id or process.returncode is not None:
                            continue
                        controls = service_controls.get(process.pid)
                        if controls is None:
                            continue

                        if not await _notify_service_stop(controls):
                            cleanup_id = '__infrastructure__/stop/' + service.start.id
                            result = CheckResult(
                                id=cleanup_id, status='indeterminate', required=True,
                                manifest_digest=manifest.digest(), context=scope,
                                output='service supervisor did not acknowledge teardown',
                            )
                            results[cleanup_id] = result
                            lifecycle.append(result)
                    result = await _execute(
                        service.stop,
                        roots=roots,
                        env=env,
                        digest=manifest.digest(),
                        scope=scope,
                        results=results,
                        ignore_dependencies=True,
                        redactions=redactions,
                    )
                    result.required = service.required
                    results[result.id] = result
                    lifecycle.append(result)
        finally:
            for process in processes:
                await _terminate(process)
                for descriptor in service_controls.pop(process.pid, ()):
                    os.close(descriptor)
                if sys.platform == 'linux':
                    start_result = results[supervisors[process.pid]]
                    start_result.exit_code = process.returncode
                    start_result.status = (
                        'pass' if process.returncode == 0 else
                        'indeterminate' if process.returncode is None or process.returncode < 0
                        or process.returncode == 125 else 'fail'
                    )
                if sys.platform == 'linux' and (
                    process.returncode is None or process.returncode < 0 or process.returncode == 125
                ):
                    cleanup_id = '__infrastructure__/cleanup/' + supervisors[process.pid]
                    result = CheckResult(
                        id=cleanup_id, status='indeterminate', required=True,
                        manifest_digest=manifest.digest(), context=scope,
                        output='service supervisor did not confirm descendant cleanup',
                    )
                    results[cleanup_id] = result
                    lifecycle.append(result)
            if drains:
                try:
                    await asyncio.wait_for(asyncio.gather(*drains), timeout=0.25)
                except TimeoutError:
                    for process in processes:
                        transport = getattr(process, '_transport', None)
                        if transport is not None:
                            transport.close()
                    result = CheckResult(
                        id='service_cleanup', status='indeterminate', required=True,
                        manifest_digest=manifest.digest(), context=scope,
                        output='service output pipes remained open after process-group cleanup',
                    )
                    results[result.id] = result
                    lifecycle.append(result)


async def run_manifest(
    manifest: EnvironmentManifest,
    *,
    cache: str | Path,
    repo: str | Path,
    scratch: str | Path,
    install: bool = False,
    scope: str = "",
    secret_names: Sequence[str] = (),
    execution_source_digest: str | None = None,
) -> VerificationReport:
    roots = {"cache": str(cache), "repo": str(repo), "scratch": str(scratch)}
    redactions = _runtime_secret_values(secret_names)
    env = manifest_environment(manifest, **roots, secret_names=secret_names)
    results: dict[str, CheckResult] = {}
    digest = manifest.digest()
    if install:
        await _commands(
            manifest.install, roots=roots, env=env, digest=digest, scope=scope, results=results, redactions=redactions,
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
    async with manifest_services(manifest, **roots, _results=results, scope=scope, secret_names=secret_names) as services:
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
                manifest.checks, roots=roots, env=env, digest=digest, scope=scope, results=results, redactions=redactions,
            )
    if not any(
        check.required and (not scope or not check.scope or scope in check.scope) for check in manifest.checks
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
        execution_source_digest=execution_source_digest,
        manifest_digest=digest,
        scope=scope,
        results=sorted(results.values(), key=lambda r: order.get(r.id, -1)),
    )


def _secret_payload() -> str:
    """Remove all transport controls before restoring values or launching children."""
    prefix = "COORDINARE_ENV_SECRET_VALUES"
    fields = {key: os.environ.pop(key) for key in tuple(os.environ)
              if key == prefix or key.startswith(prefix + "_")}
    count = fields.pop(prefix + "_COUNT", None)
    encoded = fields.pop(prefix, "")
    if count is not None:
        if not count.isascii() or not count.isdecimal() or not 1 <= int(count) <= 6 or encoded:
            raise ValueError("invalid credential fragment count")
        fragments = [fields.pop(prefix + "_" + str(index)) for index in range(int(count))]
        if any(len(fragment) > 64 * 1024 for fragment in fragments):
            raise ValueError("credential fragment exceeds transfer bound")
        encoded = "".join(fragments)
    if fields or len(encoded) > 384 * 1024:
        raise ValueError("credential payload exceeds transfer bound")
    return encoded


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
    execution_source_digest = os.environ.pop("COORDINARE_ENV_EXECUTION_SOURCE_DIGEST", None)
    secret_names: list[str] = []
    try:
        encoded_secrets = _secret_payload()
    except (ValueError, KeyError):
        parser.error('invalid transient secret environment payload')
    if encoded_secrets:
        try:
            if len(encoded_secrets) > 384 * 1024:
                raise ValueError('secret environment exceeds bounded transfer')
            supplied = json.loads(base64.b64decode(encoded_secrets, validate=True))
            if not isinstance(supplied, dict) or any(
                not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
                or key.startswith('COORDINARE_ENV_')
                or key in {'BASH_ENV', 'ENV', 'COORDINARE_REPO_DIR'}
                or not isinstance(value, str) or '\0' in value
                for key, value in supplied.items()
            ):
                raise ValueError('invalid secret environment shape')
            os.environ.update(supplied)
            secret_names = list(supplied)
        except (ValueError, UnicodeError, binascii.Error):
            parser.error('invalid transient secret environment payload')
    manifest = EnvironmentManifest.model_validate(
        json.loads(sys.stdin.read() if args.manifest_stdin else args.manifest),
    )
    report = asyncio.run(
        run_manifest(
            manifest,
            cache=args.cache,
            repo=args.repo,
            scratch=args.scratch,
            scope=args.scope, execution_source_digest=execution_source_digest,
            install=args.install,
            secret_names=secret_names,
        ),
    )
    sys.stdout.write(report.model_dump_json() + "\n")
    raise SystemExit(0 if report.passed else 1)


if __name__ == "__main__":
    main()

"""Execute accepted argv declarations; child output never controls trusted outcomes."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import functools
import json
import os
import re
import secrets
import shutil
import signal
import sys
import time
from graphlib import TopologicalSorter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import CheckResult, Command, EnvironmentManifest, VerificationReport, environment_order
from .runtime_cli import run_cli

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence

    from .runtime import RuntimeArtifactReceipt
    from .runtime_supervisor import RuntimeLease

OUTPUT_LIMIT = 4096
# Error lines earlier than the kept tail (Dettmore e2e-45: "OpenSSL is not available" scrolled out behind a long
# rubygems backtrace, so the repair saw only stack frames). A bounded excerpt keeps them in the reported output.
ERROR_EXCERPT_LINES = 8
ERROR_EXCERPT_LINE_BYTES = 240
ERROR_EXCERPT_LIMIT = 1024
_ERROR_LINE = re.compile(rb"(?i)\b(error|fatal|failed|failure|exception|not available|not found|cannot|could not|denied)\b")
_STACK_FRAME = re.compile(rb"^\s*(from\s+)?\S+:\d+:in\b|^\s+at\s")
ATTEMPT_OUTPUT_LIMIT = 256 * 1024
GENERATED_NAMES_ENV = "COORDINARE_ENV_GENERATED_NAMES"
_VALUE_REFERENCE = re.compile(r"\$\{(?P<environment>[A-Za-z_][A-Za-z0-9_]*)\}|\{(?P<root>cache|repo|scratch)\}")


# This isolated helper owns only one command's descendant tree. Its environment is
# delivered after trusted Python starts, so loader variables cannot preempt supervision.
_LINUX_SUPERVISOR = r"""
from __future__ import annotations
import contextlib
import functools
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


# Where a secret a test needs comes from when no operator supplies it (Dettmore e2e-05 to e2e-09 referenced an
# undeclared ${SECRET_KEY_BASE} and lost an install, then a repair, each time).
SECRET_GUIDANCE = ("A secret that no started service owns (for example a framework secret key) is generated inside the "
                   "command that needs it, for example NAME=$(openssl rand -hex 64) before the test command; declare "
                   "local_credentials only for services this manifest starts.")
_UNRESOLVED = re.compile(r"unresolved environment reference: ([A-Za-z_][A-Za-z0-9_]*)")


def _unresolved_name(exc: ValueError) -> str:
    """The unresolved variable's name, never a value: only _expand's own message is read."""
    match = _UNRESOLVED.fullmatch(str(exc))
    return f" ${{{match[1]}}}" if match else ""


def _expand(value: str, roots: dict[str, str], env: dict[str, str]) -> str:
    def reference(match: re.Match[str]) -> str:
        name = match.group("environment")
        if name is None:
            return roots[match.group("root")]
        if name not in env:
            raise ValueError(f"unresolved environment reference: {name}")
        return env[name]

    return _VALUE_REFERENCE.sub(reference, value)


def _command_values(command: Command, roots: dict[str, str], env: dict[str, str]) -> tuple[dict[str, str], list[str], str]:
    child_env = dict(env)
    for key in environment_order(command.env):
        child_env[key] = _expand(command.env[key], roots, child_env)
    return child_env, [_expand(arg, roots, child_env) for arg in command.argv], _expand(command.cwd, roots, child_env)


def _bound_outputs(results: dict[str, CheckResult]) -> None:
    budget = ATTEMPT_OUTPUT_LIMIT
    for result in results.values():
        output = result.output.encode()
        if len(output) > budget:
            result.output = output[:budget].decode(errors="ignore")
            result.output_truncated = True
        budget -= len(result.output.encode())


class _ErrorLines:
    """Streaming record of the latest error lines, each with the line after it (the message often follows)."""

    def __init__(self) -> None:
        self.lines: list[tuple[int, bytes]] = []
        self._partial = bytearray()
        self._offset = 0
        self._follow = False

    def feed(self, chunk: bytes) -> None:
        self._partial.extend(chunk)
        *complete, rest = bytes(self._partial).split(b"\n")
        kept = rest[-ERROR_EXCERPT_LINE_BYTES * 4:]
        self._partial = bytearray(kept)
        for line in complete:
            end = self._offset + len(line) + 1
            if self._follow and line.strip() and not _STACK_FRAME.search(line):
                self.lines.append((end, line[:ERROR_EXCERPT_LINE_BYTES]))
            self._follow = False
            if _ERROR_LINE.search(line) and not _STACK_FRAME.search(line):
                self.lines.append((end, line[:ERROR_EXCERPT_LINE_BYTES]))
                self._follow = True
            self._offset = end
            del self.lines[:-ERROR_EXCERPT_LINES]
        # A runaway partial line is trimmed; its dropped bytes still count toward later offsets.
        self._offset += len(rest) - len(kept)

    def before(self, offset: int) -> bytes:
        """Recorded lines that ended before the kept tail began."""
        return b"\n".join(line for end, line in self.lines if end <= offset)[-ERROR_EXCERPT_LIMIT:]


def _template_preflight(manifest: EnvironmentManifest, roots: dict[str, str], *, install: bool,
                        scope: str, secret_names: Sequence[str], runtime_env: Mapping[str, str] | None = None,
                        credentials: Mapping[str, str] | None = None) -> VerificationReport | None:
    """Validate against the target environment before any project command has effects."""
    commands = [*(manifest.install if install else []), *manifest.checks]
    service_required: dict[str, bool] = {}
    for service in manifest.services:
        if scope and service.start.scope and scope not in service.start.scope:
            continue
        lifecycle = [*([service.init] if service.init else []), service.start, service.health, service.stop]
        commands.extend(lifecycle)
        service_required.update((command.id, service.required) for command in lifecycle)
    digest = manifest.digest()
    results = {command.id: CheckResult(id=command.id, required=service_required.get(command.id, command.required),
        status="not_run", output="template preflight did not pass", manifest_digest=digest, context=scope)
        for command in commands if not scope or not command.scope or scope in command.scope}
    try:
        env = manifest_environment(manifest, **roots, secret_names=secret_names, runtime_env=runtime_env, credentials=credentials)
    except ValueError as exc:
        # Avoid including environment values in diagnostics from failed activation; only a variable name is named.
        results["activation"] = CheckResult(id="activation", required=True, status="fail",
            output="activation template expansion failed" + _unresolved_name(exc) + "; use {cache}, {repo}, {scratch} for "
                   "roots. Activation may reference only operator-supplied runtime variables, never a generated "
                   "credential. " + SECRET_GUIDANCE, manifest_digest=digest, context=scope)
    else:
        for command in commands:
            if command.id not in results:
                continue
            try:
                _command_values(command, roots, env)
            except ValueError as exc:
                if not results[command.id].required:
                    continue
                results[command.id].status = "fail"
                results[command.id].output = (
                    "unresolved environment reference" + _unresolved_name(exc) + " in command template; use {cache}, "
                    "{repo}, {scratch} for execution roots. ${NAME} requires a runtime environment variable; use $name for "
                    "shell-local variables assigned inside the command. " + SECRET_GUIDANCE
                )
    if any(result.required and result.status == "fail" for result in results.values()):
        _bound_outputs(results)
        return VerificationReport(manifest_digest=digest, scope=scope, results=list(results.values()))
    return None


def manifest_environment(
    manifest: EnvironmentManifest,
    *,
    cache: str | Path,
    repo: str | Path,
    scratch: str | Path,
    secret_names: Sequence[str] = (),
    runtime_env: Mapping[str, str] | None = None,
    credentials: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Apply only declared activation, without executing cache scripts or starting services."""
    roots = {"cache": str(cache), "repo": str(repo), "scratch": str(scratch)}
    env = dict(os.environ)
    try:
        existing_names = json.loads(env.get('COORDINARE_ENV_SECRET_NAMES', '[]'))
    except ValueError:
        existing_names = []
    inherited = {name for name in existing_names if isinstance(name, str)} if isinstance(existing_names, list) else set()
    generated = dict(credentials or {})
    env['COORDINARE_ENV_SECRET_NAMES'] = json.dumps(sorted(inherited | set(secret_names) | set(generated)))
    # Bash startup hooks must never run before an explicit declared command.
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    if runtime_env is not None:
        env.update({key: value for key, value in runtime_env.items() if key != "PATH"})
    if set(generated) & set(runtime_env or {}):
        raise ValueError("local_credential_conflict")
    env.update(generated)
    if generated:
        env[GENERATED_NAMES_ENV] = json.dumps(sorted(generated))
    for key in environment_order(manifest.activation.env):
        env[key] = _expand(manifest.activation.env[key], roots, env)
    paths = [_expand(path, roots, env) for path in manifest.activation.path]
    env["PATH"] = os.pathsep.join(dict.fromkeys(
        path for path in [*paths, *env.get("PATH", "").split(os.pathsep)] if path
    ))
    if runtime_env is not None:
        prefix = runtime_env.get("PATH", "").split(os.pathsep)[0]
        env["PATH"] = os.pathsep.join(dict.fromkeys(part for part in [prefix, *env["PATH"].split(os.pathsep)] if part))
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
            # Python 3.12's Process.send_signal() calls Popen.poll(), which can
            # reap an exited supervisor before asyncio's child watcher and lose
            # its status (reported as 255). Leave reaping to the watcher.
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


# Commands stop before a private runtime's teardown allowance (see runtime_execution). Service stops still run after
# that point, for residual-state cleanup, each within this window past the command deadline.
SESSION_DEADLINE_NOT_RUN = 'session deadline reached before this command ran'
TEARDOWN_STOP_SECONDS = 60.0


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
    finalizers: dict[int, Callable[[], str]] | None = None,
    budget: float | None = None,
    teardown: bool = False,
) -> CheckResult:
    """``budget`` is a monotonic deadline for the command: once it has passed a non-teardown command does not run, and a
    command still running at it is cut short. A teardown command runs regardless, with at least a second."""
    result = CheckResult(
        id=command.id,
        status="not_run",
        required=command.required,
        manifest_digest=digest,
        context=scope,
    )
    if budget is not None and not teardown and time.monotonic() >= budget:
        result.output = SESSION_DEADLINE_NOT_RUN
        return result
    if not ignore_dependencies and any(
        key not in results or results[key].status != "pass" for key in command.depends_on
    ):
        result.output = "prerequisite did not pass"
        return result
    timeout, cut_short = command.timeout_seconds, False
    if budget is not None:
        left = budget - time.monotonic()
        timeout = min(timeout, max(left, 1.0) if teardown else left)
        cut_short = timeout < command.timeout_seconds
    started = time.monotonic()
    process = None
    ack_read = ack_write = None
    control_read = control_write = None
    drain = None
    tail = bytearray()
    total = 0
    errors = _ErrorLines()
    child_env = dict(env)
    known_secrets = frozenset(_secret_values(env)) | redactions
    retained_limit = OUTPUT_LIMIT * 2 + max((len(value.encode()) for value in known_secrets), default=0)
    try:
        child_env, argv, cwd = _command_values(command, roots, child_env)
        retained_limit = max(retained_limit, OUTPUT_LIMIT * 2 + max(
            (len(value.encode()) for value in _secret_values(child_env)), default=0,
        ))
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
                errors.feed(chunk)
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
                if finalizers is not None:
                    # Dettmore e2e-54: start-maxio exited 1 after this first moment, and its result kept only the
                    # output of the first 0.1 s (none). The drain keeps reading; teardown takes its final output.
                    def final_output() -> str:
                        cleaned = _redact(tail.decode(errors="replace"), child_env, extra_values=known_secrets)
                        return cleaned.encode()[-OUTPUT_LIMIT:].decode(errors="ignore")
                    finalizers[process.pid] = final_output
                if sys.platform == 'linux':
                    result.status = "pass"
                else:
                    # Process-group cleanup cannot prove a running service's
                    # successful completion; only the Linux supervisor can.
                    result.status = "indeterminate"
                    tail.extend(b"\nbackground service completion requires Linux supervision")
                return result
        try:
            await asyncio.wait_for(process.wait(), timeout)
            result.exit_code = process.returncode
            result.status = "pass" if process.returncode == 0 else "fail"
        except TimeoutError:
            result.status = "indeterminate"
            tail.extend(b"\ncommand cut short: session deadline reached" if cut_short else b"\ncommand timed out")
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
        output = cleaned[-OUTPUT_LIMIT:]
        earlier = errors.before(total - len(output)) if result.output_truncated else b""
        if earlier:
            excerpt = _redact(earlier.decode(errors="replace"), child_env, extra_values=known_secrets).encode()
            excerpt = b"[error lines from earlier output]\n" + excerpt[-ERROR_EXCERPT_LIMIT:] + b"\n[... output tail]\n"
            output = excerpt + output[-(OUTPUT_LIMIT - len(excerpt)):]
        if result.status == "fail" and (logs := _referenced_logs(output.decode(errors="ignore"), roots)) is not None:
            excerpt = _redact(logs.decode(errors="replace"), child_env, extra_values=known_secrets).encode()
            output = (excerpt + b"\n[... output]\n")[:FAILED_OUTPUT_LIMIT - len(output)] + output
        result.output = output.decode(errors="ignore")
    return result


# Dettmore e2e-45..51: ruby-build prints only "BUILD FAILED ... See the full build log at /tmp/ruby-build.N.log"; the
# cause stayed in that file, and two repair generations were spent without it. Dettmore e2e-54: that log in turn named
# ext/openssl/mkmf.log and three more, absolute or relative to the source tree, which hold why each extension was not
# configured. A failed command's output gets the error lines of every log it (or the first log) names under /tmp or
# the cache or scratch roots, then the raw tail of the first, redacted, before its own bounded output.
REFERENCED_LOG_LIMIT = 2048
REFERENCED_ERROR_LIMIT = 1536
REFERENCED_LOG_READ = 64 * 1024
MAX_REFERENCED_LOGS = 8
REFERENCED_ERROR_LINES = 6
FAILED_OUTPUT_LIMIT = OUTPUT_LIMIT * 2
_REFERENCED_LOG = re.compile(r"build log (?:at|in) (/[A-Za-z0-9._/+-]{1,240}\.log)\b")
_ABSOLUTE_LOG = re.compile(r"(?<![A-Za-z0-9._/+-])(/[A-Za-z0-9._/+-]{1,240}\.log)\b")
_RELATIVE_LOG = re.compile(r"(?<![A-Za-z0-9._/+-])((?:[A-Za-z0-9._+-]+/){1,6}[A-Za-z0-9._+-]+\.log)\b")
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9._/+-])(/[A-Za-z0-9._/+-]{1,240})")
_LOG_ERROR_LINE = re.compile(rb"(?i)could not be found|not found|no such file|loaderror|cannot (?:find|load|open)|"
                             rb"undefined reference|not available|could not|\berror\b|\bfatal\b|\bmissing\b")


def _allowed_log(path: str, roots: Mapping[str, str]) -> Path | None:
    if ".." in path.split("/"):
        return None
    bases = ["/tmp", *(roots[key] for key in ("cache", "scratch") if roots.get(key))]
    if not any(path == base or path.startswith(base.rstrip("/") + "/") for base in bases):
        return None
    candidate = Path(path)
    try:
        # Resolved, so a symlinked parent (ln -s / /tmp/r) cannot reach outside the allowed roots.
        resolved = candidate.resolve(strict=True)
        if candidate.is_symlink() or not resolved.is_file() or not any(
                resolved.is_relative_to(Path(base).resolve()) for base in bases):
            return None
    except (OSError, RuntimeError):
        return None
    return resolved


def _named_logs(text: str, roots: Mapping[str, str]) -> list[Path]:
    """Logs a text names: absolute paths, then relative ones resolved under a directory another path there names."""
    found: list[Path] = []
    for match in [*_REFERENCED_LOG.finditer(text), *_ABSOLUTE_LOG.finditer(text)]:
        if (path := _allowed_log(match.group(1), roots)) is not None and path not in found:
            found.append(path)
    bases = list(dict.fromkeys(str(parent) for match in _ABSOLUTE_PATH.finditer(text)
                               for parent in list(Path(match.group(1)).parents)[:6] if str(parent) != "/"))[:64]
    for match in _RELATIVE_LOG.finditer(text):
        relative = match.group(1)
        for base in bases:
            if (path := _allowed_log(f"{base}/{relative}", roots)) is not None:
                if path not in found:
                    found.append(path)
                break
    return found


def _read_tail(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, handle.seek(0, os.SEEK_END) - limit))
            return handle.read()
    except OSError:
        return b""


def _referenced_logs(output: str, roots: Mapping[str, str]) -> bytes | None:
    logs = _named_logs(output, roots)
    if not logs:
        return None
    first = _read_tail(logs[0], REFERENCED_LOG_READ)
    for path in _named_logs(first.decode(errors="ignore"), roots):
        if path not in logs:
            logs.append(path)
    errors: list[bytes] = []
    for path in logs[:MAX_REFERENCED_LOGS]:
        content = first if path == logs[0] else _read_tail(path, REFERENCED_LOG_READ)
        lines = [line.strip()[:ERROR_EXCERPT_LINE_BYTES] for line in content.split(b"\n")
                 if _LOG_ERROR_LINE.search(line) and not _STACK_FRAME.search(line)]
        lines = list(dict.fromkeys(lines))[-REFERENCED_ERROR_LINES:]
        if lines:
            errors.append(f"{path}:".encode() + b"".join(b"\n  " + line for line in lines))
    excerpt = b""
    if errors:
        excerpt = (b"[error lines from the logs this output names]\n"
                   + b"\n".join(errors)[:REFERENCED_ERROR_LIMIT] + b"\n")
    return excerpt + b"[tail of the build log this output names]\n" + first[-REFERENCED_LOG_LIMIT:]


# A trivial command run with the activated environment after each install step. Maintainer ruling 2026-10-31: one
# general health check, not a rule per setting. Dettmore e2e-39: an install step copied an old libc into a directory
# activation put on LD_LIBRARY_PATH, and every later command, the shell included, failed; nothing named the step.
# sh, not bash: every image has a POSIX shell, and anything that breaks it (a replaced libc or PATH) breaks bash too.
ENVIRONMENT_HEALTH = Command(id="environment-health", argv=["sh", "-c", "true"], cwd="{repo}", timeout_seconds=30)
UNHEALTHY = ("environment health check failed after this step: a trivial command (sh -c true) could not run with "
             "the activated environment, so later commands will fail too. Keep this step from breaking the shell "
             "(for example a library or PATH entry that replaces a system one): ")


async def _environment_health(*, roots: dict[str, str], env: dict[str, str], digest: str, scope: str,
                              redactions: frozenset[str]) -> str | None:
    """None when a trivial command still runs with the activated environment; otherwise its first output line."""
    probe = await _execute(ENVIRONMENT_HEALTH, roots=roots, env=env, digest=digest, scope=scope, results={},
                           ignore_dependencies=True, redactions=redactions)
    if probe.status == "pass":
        return None
    first = next((line.strip() for line in probe.output.splitlines() if line.strip()), "")
    return (first or f"exit {probe.exit_code}")[:400]


async def _commands(
    commands: list[Command],
    *,
    roots: dict[str, str],
    env: dict[str, str],
    digest: str,
    scope: str,
    results: dict[str, CheckResult],
    redactions: frozenset[str] = frozenset(),
    health: bool = False,
    budget: float | None = None,
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
                    budget=budget,
                )
                if health and results[command.id].status in {"pass", "fail"} and (broken := await _environment_health(
                        roots=roots, env=env, digest=digest, scope=scope, redactions=redactions)) is not None:
                    result = results[command.id]
                    result.status = "fail"
                    result.output = UNHEALTHY + broken + ("\n" + result.output if result.output else "")
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
    runtime_env: Mapping[str, str] | None = None,
    credentials: Mapping[str, str] | None = None,
    command_deadline: float | None = None,
) -> AsyncIterator[list[CheckResult]]:
    """Hold explicitly declared services until exit; expose lifecycle failures to callers.

    ``command_deadline`` bounds init, start and health like the checks; stops run after it, within TEARDOWN_STOP_SECONDS.
    """
    stop_budget = None if command_deadline is None else command_deadline + TEARDOWN_STOP_SECONDS
    roots = {"cache": str(cache), "repo": str(repo), "scratch": str(scratch)}
    redactions = _runtime_secret_values(secret_names) | frozenset((credentials or {}).values())
    env = manifest_environment(manifest, **roots, secret_names=secret_names, runtime_env=runtime_env, credentials=credentials)
    results = _results if _results is not None else {}
    lifecycle: list[CheckResult] = []
    started = []
    processes: list[asyncio.subprocess.Process] = []
    drains: list[asyncio.Task[None]] = []
    supervisors: dict[int, str] = {}
    service_controls: dict[int, tuple[int, int]] = {}
    finalizers: dict[int, Callable[[], str]] = {}
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
                        finalizers=finalizers,
                        budget=command_deadline,
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
                if healthy and result.status in {"fail", "indeterminate"} and (
                        logs := await _service_logs(service, roots=roots, env=env, redactions=redactions)):
                    result.output = result.output + ("\n" if result.output and not result.output.endswith("\n") else "") + logs
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
                        budget=stop_budget,
                        teardown=True,
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
            for pid, final_output in finalizers.items():
                start_result = results.get(supervisors.get(pid, ""))
                if start_result is not None and start_result.status != "pass" and (late := final_output()):
                    # The late output replaces the early capture it extends; service logs appended after it stay.
                    marker = _SERVICE_LOG_MARKER.search(start_result.output)
                    start_result.output = late + (start_result.output[marker.start():] if marker else "")


# Dettmore e2e-54: a service step failed with "maxio-mock not ready" or nothing at all, while the reason was in the
# service's own log: a container's (docker logs) or a file its start command writes. Both reach the failed step.
SERVICE_LOG_LINES = 40
_SERVICE_LOG_MARKER = re.compile(r"\n\[(?:docker logs --tail \d+ |tail of /)")
SERVICE_LOG_LIMIT = 2048
MAX_SERVICE_LOGS = 3


async def _service_logs(service: Any, *, roots: dict[str, str], env: dict[str, str], redactions: frozenset[str]) -> str:
    from .runtime_execution import docker_created

    parts: list[str] = []
    commands = [command for command in (service.init, service.start) if command is not None]
    expanded: list[list[str]] = []
    for command in commands:
        try:
            child_env, argv, _ = _command_values(command, roots, dict(env))
        except ValueError:
            continue
        expanded.append(argv)
        # Secret values expanded into a service command's own env are redacted too.
        redactions = redactions | frozenset(_secret_values(child_env))
    names = sorted({name for command in commands for name in docker_created(command.argv)["containers"]})
    docker = shutil.which("docker", path=env.get("PATH")) if names else None
    for name in names[:MAX_SERVICE_LOGS] if docker else ():
        try:
            process = await asyncio.create_subprocess_exec(
                docker, "logs", "--tail", str(SERVICE_LOG_LINES), name, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except OSError:
            continue
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 10)
        except TimeoutError:
            await _terminate(process)
            continue
        if process.returncode == 0 and output.strip():
            parts.append(f"[docker logs --tail {SERVICE_LOG_LINES} {name}]\n"
                         + output.decode(errors="replace")[-SERVICE_LOG_LIMIT:])
    for argv in expanded:
        for path in _named_logs(" ".join(argv), roots)[:MAX_SERVICE_LOGS]:
            if tail := _read_tail(path, SERVICE_LOG_LIMIT).decode(errors="replace"):
                parts.append(f"[tail of {path}]\n{tail}")
    return _redact("\n".join(parts[:MAX_SERVICE_LOGS * 2]), env, extra_values=redactions)


async def _run_manifest_commands(
    manifest: EnvironmentManifest,
    *,
    cache: str | Path,
    repo: str | Path,
    scratch: str | Path,
    install: bool = False,
    scope: str = "",
    secret_names: Sequence[str] = (),
    execution_source_digest: str | None = None,
    runtime_env: Mapping[str, str] | None = None,
    credentials: Mapping[str, str] | None = None,
    progress: dict[str, CheckResult] | None = None,
    command_deadline: float | None = None,
) -> VerificationReport:
    """``progress``, when given, receives each result as it lands, so an owner keeps finished results if cancelled.
    ``command_deadline`` (monotonic) bounds the install, lifecycle and check commands; see ``_execute``."""
    roots = {"cache": str(cache), "repo": str(repo), "scratch": str(scratch)}
    redactions = _runtime_secret_values(secret_names) | frozenset((credentials or {}).values())
    preflight = _template_preflight(manifest, roots, install=install, scope=scope, secret_names=secret_names,
                                    runtime_env=runtime_env, credentials=credentials)
    if preflight is not None:
        preflight.execution_source_digest = execution_source_digest
        return preflight
    env = manifest_environment(manifest, **roots, secret_names=secret_names, runtime_env=runtime_env, credentials=credentials)
    results: dict[str, CheckResult] = progress if progress is not None else {}
    digest = manifest.digest()
    if install:
        await _commands(
            manifest.install, roots=roots, env=env, digest=digest, scope=scope, results=results, redactions=redactions,
            health=True, budget=command_deadline,
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
    # Dettmore e2e-54: with install-ruby failed, every generation still started the services, whose ruby-based mock
    # failed with "env: 'ruby': No such file or directory" and read as a fault of its own. A service starts only when
    # every required install passed; otherwise its steps are not_run and name the installs.
    def in_scope(command: Command) -> bool:
        return not (scope and command.scope and scope not in command.scope)

    blocked = [command.id for command in manifest.install
               if install and command.required and in_scope(command)
               and (command.id not in results or results[command.id].status != "pass")]
    if blocked:
        note = "required install did not pass: " + ", ".join(blocked[:8])
        for service in manifest.services:
            if scope and service.start.scope and scope not in service.start.scope:
                continue
            for command in (service.init, service.start, service.health, service.stop):
                if command is not None:
                    results[command.id] = CheckResult(id=command.id, status="not_run", required=service.required,
                                                      output=note, manifest_digest=digest, context=scope)
        for check in manifest.checks:
            results[check.id] = CheckResult(
                id=check.id, status="not_run", required=check.required and in_scope(check),
                output="environment prerequisite failed" if in_scope(check) else "outside requested scope",
                manifest_digest=digest, context=scope)
    else:
        async with manifest_services(manifest, **roots, _results=results, scope=scope, secret_names=secret_names,
                                     runtime_env=runtime_env, credentials=credentials,
                                     command_deadline=command_deadline) as services:
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
                    redactions=redactions, budget=command_deadline,
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
    _bound_outputs(results)
    order = {
        command.id: index for index, command in enumerate([*manifest.install, *manifest.checks])
    }
    return VerificationReport(
        execution_source_digest=execution_source_digest,
        manifest_digest=digest,
        scope=scope,
        results=sorted(results.values(), key=lambda r: order.get(r.id, -1)),
    )


async def run_manifest(
    manifest: EnvironmentManifest, *, cache: str | Path, repo: str | Path, scratch: str | Path,
    install: bool = False, scope: str = "", secret_names: Sequence[str] = (),
    execution_source_digest: str | None = None,
    runtime_artifacts: RuntimeArtifactReceipt | None = None,
    runtime_deadline: float | None = None, runtime_lease: RuntimeLease | None = None,
    credentials: Mapping[str, str] | None = None,
) -> VerificationReport:
    try:
        if credentials is None:
            credentials = local_credential_values(manifest, supplied=secret_names)
        elif set(credentials) != {credential.name for credential in manifest.local_credentials}:
            raise ValueError("local_credential_conflict")
    except ValueError:
        return VerificationReport(execution_source_digest=execution_source_digest, manifest_digest=manifest.digest(),
            scope=scope, results=[CheckResult(id="local_credentials", required=True, status="fail",
                output="a declared local credential name is also supplied as a runtime secret",
                manifest_digest=manifest.digest(), context=scope)])
    commands = functools.partial(_run_manifest_commands, credentials=credentials)
    if manifest.runtime is not None:
        from .runtime_execution import execute_with_runtime

        return await execute_with_runtime(manifest, cache=cache, repo=repo, scratch=scratch,
            install=install, scope=scope, secret_names=secret_names, execution_source_digest=execution_source_digest,
            runtime_artifacts=runtime_artifacts, runtime_deadline=runtime_deadline, runtime_lease=runtime_lease,
            commands=commands, redact_values=frozenset(credentials.values()))
    if runtime_lease is not None or runtime_artifacts is not None:
        raise ValueError("native manifest cannot use private runtime evidence")
    return await commands(manifest, cache=cache, repo=repo, scratch=scratch,
        install=install, scope=scope, secret_names=secret_names, execution_source_digest=execution_source_digest)


def local_credential_values(manifest: EnvironmentManifest, *, supplied: Collection[str]) -> dict[str, str]:
    """Fresh values for credentials of services the manifest starts; never persisted, never operator secrets."""
    names = [credential.name for credential in manifest.local_credentials]
    # A generated value must never stand in for an operator secret or shadow an inherited variable.
    # Names an enclosing runner generated (a job's verify subprocess) are regenerated, not conflicts.
    try:
        enclosing = set(json.loads(os.environ.get(GENERATED_NAMES_ENV, "[]")))
    except (TypeError, ValueError):
        enclosing = set()
    if set(names) & (set(supplied) | (set(os.environ) - enclosing)):
        raise ValueError("local_credential_conflict")
    return {name: secrets.token_urlsafe(32) for name in names}


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
    from .runtime_transport import consume_runtime_controls

    runtime_deadline, runtime_artifacts = consume_runtime_controls(required=manifest.runtime is not None, install=args.install)
    from .runtime_execution import PrivateRuntimeIncompleteError

    try:
        report = run_cli(
            run_manifest(
                manifest,
                cache=args.cache,
                repo=args.repo,
                scratch=args.scratch,
                scope=args.scope, execution_source_digest=execution_source_digest,
                install=args.install,
                secret_names=secret_names,
                runtime_deadline=runtime_deadline, runtime_artifacts=runtime_artifacts,
            ),
        )
    except PrivateRuntimeIncompleteError as exc:
        # The run holds either way; emit the evidence instead of only a traceback.
        if exc.report is None:
            raise
        report = exc.report
    sys.stdout.write(report.model_dump_json() + "\n")
    raise SystemExit(0 if report.passed and not report.runtime_incomplete else 1)


if __name__ == "__main__":
    main()

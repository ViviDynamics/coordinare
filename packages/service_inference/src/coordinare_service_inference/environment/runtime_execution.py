"""Execute a manifest with one owned runtime across commands and teardown."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from .runtime_io import settle as _settle
from .runtime_supervisor import RuntimeScanStoppedError, open_runtime, seal_runtime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from .models import CheckResult, EnvironmentManifest, VerificationReport
    from .runtime import RuntimeArtifactReceipt
    from .runtime_supervisor import RuntimeLease


TEARDOWN_CONDITIONS = ('qemu_reaped', 'keys_absent', 'listeners_absent', 'artifacts_unchanged', 'clean_shutdown',
                       'bounds_respected')


class PrivateRuntimeIncompleteError(RuntimeError):
    """The run cannot be accepted (deadline, seal or teardown): it holds, but keeps its evidence and the reasons."""

    def __init__(self, failed: list[str], report: VerificationReport | None) -> None:
        super().__init__('private runtime run incomplete: ' + ', '.join(failed))
        self.failed = failed
        self.report = report


DETAIL_MAX_CHARS = 16000


def _cause_detail(cause: BaseException, secret_names: Sequence[str], extra: frozenset[str] = frozenset()) -> str:
    """Exception class and message with known secret values removed, never clipped: only the owner can
    pattern-redact, and any cut made before that could split a credential into an unrecognisable fragment. A
    message beyond the bound is replaced by the exception class alone."""
    from .runner import _redact, _runtime_secret_values

    text = _redact(f'{type(cause).__name__}: {cause}', {}, extra_values=_runtime_secret_values(secret_names) | extra)
    return text if len(text) <= DETAIL_MAX_CHARS else type(cause).__name__


# What the guest's Docker still holds once every command and service stop has run, by name so an author can tell
# which record left it. The seal check refuses to shut down while any of it remains.
# Each listing is assigned under set -e, so any failed listing fails the whole script rather than reading as empty.
RESIDUAL_LIST = ("set -eu; c=$(docker ps -a --format '{{.Names}}'); v=$(docker volume ls -q); "
                 "n=$(docker network ls --filter type=custom --format '{{.Name}}'); "
                 "for kind in containers volumes networks; do case $kind in containers) x=$c;; volumes) x=$v;; "
                 "networks) x=$n;; esac; printf '%s=%s\\n' \"$kind\" \"$(printf '%s' \"$x\" | tr '\\n' ' ')\"; done")
RESIDUAL_REMOVE = ("docker ps -aq | xargs -r docker rm -f -v >/dev/null; docker volume ls -q | xargs -r docker volume rm -f "
                   ">/dev/null; docker network ls --filter type=custom -q | xargs -r docker network rm >/dev/null")
RESIDUAL_GUIDANCE = ('Every service stop must remove the Docker state its service created, including anonymous volumes '
                     '(docker rm -f -v) and custom networks: the guest cannot shut down cleanly while any remain.')
COMMAND_RESIDUAL_GUIDANCE = ('A command that creates Docker state must remove it before it exits, when it fails too '
                             '(docker run --rm, or remove it in a trap): the guest cannot shut down cleanly while any '
                             'remains.')
MAX_RESIDUAL_NAMES = 16


async def _reclaim_residual_state(lease: RuntimeLease, manifest: EnvironmentManifest, report: VerificationReport, *,
                                  deadline: float, secret_names: Sequence[str], redact_values: frozenset[str]) -> None:
    """Docker state left after the last command fails the records that should have removed it, then is removed, so the
    guest still shuts down cleanly and the run stays repairable.

    The seal check would otherwise refuse the shutdown and hold the run as incomplete, with no cause and no repair
    (Dettmore e2e-04: a postgres container's anonymous volume). Each name a command that ran explicitly created blames
    that command, or its service's stop; the rest blame the service stops that ran, or failing those the commands
    that ran Docker. State no record could have left is not removed: the seal check still refuses it.
    Resource names are guest output, so they are redacted like command output before they reach a result.
    """
    from .runner import _redact, _runtime_secret_values, _secret_values

    try:
        _code, output = await lease.ssh(RESIDUAL_LIST, deadline=deadline)
    except (RuntimeError, OSError, TimeoutError):
        return
    left = {kind: names.split() for kind, _, names in
            (line.partition('=') for line in output.decode(errors='replace').splitlines()) if names.split()}
    if not left:
        return
    ran = {result.id: result for result in report.results if result.status != 'not_run'}
    blamed, unexplained = _residual_creators(manifest, ran, left)
    fallback = [service.stop.id for service in manifest.services if service.stop.id in ran] or [
        command.id for command in [*manifest.install, *manifest.checks]
        if command.id in ran and any('docker' in word for word in command.argv)]
    for record in fallback if unexplained else []:
        for kind, unclaimed in unexplained.items():
            blamed.setdefault(record, {}).setdefault(kind, []).extend(unclaimed)
    if not blamed:
        return
    stops = {service.stop.id for service in manifest.services}
    # The same secret set command output is redacted with, plus the run's generated credentials and activation.
    known = _runtime_secret_values(secret_names) | redact_values | frozenset(_secret_values(dict(lease.activation_env)))
    for record, state in blamed.items():
        result = ran[record]
        found = '; '.join(f'{kind} {" ".join(listed[:MAX_RESIDUAL_NAMES])}' for kind, listed in state.items())
        guidance = RESIDUAL_GUIDANCE if record in stops else COMMAND_RESIDUAL_GUIDANCE
        detail = _redact(f'teardown found Docker state left behind: {found}. {guidance}', {}, extra_values=known)
        result.status = 'fail'
        result.output = f'{result.output}\n{detail}' if result.output else detail
    if unexplained and not fallback:
        # Some state no record answers for: it stays, and the seal check refuses it.
        return
    with contextlib.suppress(RuntimeError, OSError, TimeoutError):
        await lease.ssh(RESIDUAL_REMOVE, deadline=deadline, check=False)


def _residual_creators(manifest: EnvironmentManifest, ran: dict[str, CheckResult], left: dict[str, list[str]],
                       ) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    """Leftover names a command that ran created with its own docker invocation, keyed by the record to blame, and
    the rest.

    Dettmore e2e-12: install-host-ruby failed under set -e between `docker create --name ruby-extract` and its
    `docker rm`, and every service stop was blamed for the container, sending repair after stops that had worked.
    A service's step blames that service's stop when the stop ran, and otherwise the step itself.
    """
    creators = [(command, command.id) for command in [*manifest.install, *manifest.checks]]
    creators += [(command, service.stop.id if service.stop.id in ran else command.id)
                 for service in manifest.services
                 for command in (getattr(service, step, None) for step in ('init', 'start', 'health', 'stop'))
                 if command is not None]
    made = [(record, docker_created(command.argv)) for command, record in creators if command.id in ran]
    blamed: dict[str, dict[str, list[str]]] = {}
    unexplained: dict[str, list[str]] = {}
    for kind, names in left.items():
        for name in names:
            records = sorted({record for record, created in made if name in created[kind]})
            for record in records:
                blamed.setdefault(record, {}).setdefault(kind, []).append(name)
            if not records:
                unexplained.setdefault(kind, []).append(name)
    return blamed, unexplained


class _Options(NamedTuple):
    """How one docker subcommand reads its words: the options that take no value, and whether options may follow a
    positional word (run and create stop at the image, as docker does)."""

    bare_long: frozenset[str]
    bare_short: frozenset[str]
    interspersed: bool


_RUNS = (['run'], ['create'], ['container', 'run'], ['container', 'create'])
_RUN_OPTIONS = _Options(frozenset({'--detach', '--rm', '--interactive', '--tty', '--privileged', '--init',
                                   '--publish-all', '--read-only', '--no-healthcheck', '--oom-kill-disable',
                                   '--sig-proxy', '--quiet', '--help', '--disable-content-trust', '--use-api-socket'}),
                        frozenset('ditPq'), interspersed=False)
# volume create takes a value for every option, -d (driver) included; network create has a few bare long flags.
_CREATES = {'volume create': ('volumes', _Options(frozenset({'--help'}), frozenset(), interspersed=True)),
            'network create': ('networks', _Options(frozenset({'--help', '--attachable', '--config-only', '--ingress',
                                                               '--internal', '--ipv4', '--ipv6'}),
                                                    frozenset(), interspersed=True))}


def docker_created(argv: Sequence[str]) -> dict[str, set[str]]:
    """The Docker state a command names as it creates it, read from its docker invocations as shell words.

    Never invents: a missed creator only falls back to the earlier attribution, while an invented one would blame an
    innocent command and remove state nothing answers for. So a script the carrier lexer refuses (a heredoc, a quoted
    or escaped operator) names nothing, nor does one whose commands might not run as written (control flow, a function,
    a subshell or substitution, a comment) or whose quoted words hold redirection characters the lexer would drop. Only
    a docker invocation counts, read as docker reads it: run and create stop at the image and need one, a later --name
    wins, --help creates nothing, and a create names only its positional word.
    """
    from .static_rules import _program, _script, _simple_commands

    created: dict[str, set[str]] = {'containers': set(), 'volumes': set(), 'networks': set()}
    script = _joined_continuations(_script(argv))
    if _UNREAD.search(script) or _QUOTED_REDIRECTION.search(script):
        return created
    commands = [_program(words) for words in _simple_commands(script)]
    if any(program and program[0] in _CONTROL for program in commands):
        return created
    for program in commands:
        if not program or program[0].rsplit('/', 1)[-1] != 'docker':
            continue
        args = program[1:]
        run = next((form for form in _RUNS if args[:len(form)] == form), None)
        if run is not None:
            options, positional = _parse(args[len(run):], _RUN_OPTIONS)
            if positional and not _helped(options):
                _record_run(options, created)
        elif (create := _CREATES.get(' '.join(args[:2]))) is not None:
            kind, spec = create
            options, positional = _parse(args[2:], spec)
            # A create with no name asks docker to generate one: nothing is named.
            if positional and not _helped(options):
                created[kind].add(positional[0])
    return created


# Script text after which a command may not run as written, or the lexer would not see it: a subshell, function or
# substitution, a comment, a backquote.
_UNREAD = re.compile(r'[()#`]')
# A quoted word holding < or >: the lexer would read it as a redirection and drop it.
_QUOTED_REDIRECTION = re.compile('\'[^\']*[<>][^\']*\'|"[^"]*[<>][^"]*"')
_CONTROL = frozenset({'if', 'then', 'else', 'elif', 'fi', 'case', 'esac', 'for', 'select', 'while', 'until', 'do',
                      'done', 'function', '!', '[[', 'coproc'})


def _joined_continuations(script: str) -> str:
    """The script with each backslash-newline the shell removes removed: outside single quotes, and only where the
    backslash is not itself escaped."""
    out: list[str] = []
    quote = ''
    index = 0
    while index < len(script):
        character = script[index]
        if quote != "'" and character == '\\':
            if script[index + 1:index + 2] == '\n':
                index += 2
                continue
            out.append(script[index:index + 2])
            index += 2
            continue
        if character in '\'"' and quote in ('', character):
            quote = '' if quote else character
        out.append(character)
        index += 1
    return ''.join(out)


def _helped(options: list[tuple[str, str]]) -> bool:
    return any(option in {'--help', '-h'} for option, _value in options)


def _record_run(options: list[tuple[str, str]], created: dict[str, set[str]]) -> None:
    """A run or create's container name (the last --name wins) and the named volumes its mounts create."""
    names = [value for option, value in options if option == '--name']
    if names:
        created['containers'].add(names[-1])
    for option, value in options:
        if option in {'-v', '--volume'} and (volume := _named_volume(value.split(':', 1)[0])):
            created['volumes'].add(volume)
        elif option == '--mount':
            kind, source = 'volume', ''
            for field in value.split(','):
                key, _, setting = field.partition('=')
                if key == 'type':
                    kind = setting
                elif key in {'source', 'src'}:
                    # Docker applies the fields in order: the last source wins.
                    source = setting
            if kind == 'volume' and _named_volume(source):
                created['volumes'].add(source)


def _parse(args: list[str], spec: _Options) -> tuple[list[tuple[str, str]], list[str]]:
    """Docker's own reading of a subcommand's words: each option with its value, and the positional words."""
    options: list[tuple[str, str]] = []
    positional: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        index += 1
        if word == '--':
            positional.extend(args[index:])
            break
        if word.startswith('--'):
            name, equals, value = word.partition('=')
            if not equals and name not in spec.bare_long and index < len(args):
                value, index = args[index], index + 1
            options.append((name, value))
        elif word.startswith('-') and len(word) > 1:
            letters = word[1:]
            if set(letters) <= spec.bare_short:
                continue
            flag = next(position for position, letter in enumerate(letters) if letter not in spec.bare_short)
            name, value = f'-{letters[flag]}', letters[flag + 1:]
            if not value and index < len(args):
                value, index = args[index], index + 1
            options.append((name, value))
        else:
            positional.append(word)
            if not spec.interspersed:
                positional.extend(args[index:])
                break
    return options, positional


def _named_volume(source: str) -> str:
    """A -v or --mount source that names a volume, never a host path."""
    return source if source and not source.startswith(('/', '.', '~', '$')) and '/' not in source else ''


def _cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _partial_report(manifest: EnvironmentManifest, progress: dict[str, CheckResult], *, install: bool, scope: str,
                    execution_source_digest: str) -> VerificationReport:
    """Finished results as recorded; every declared command, service step included, without one is not_run."""
    from .models import CheckResult, VerificationReport
    from .runner import _bound_outputs

    digest = manifest.digest()
    declared = [*(manifest.install if install else []), *manifest.checks,
                *(command for service in manifest.services
                  for command in (service.init, service.start, service.health, service.stop) if command is not None)]
    results = dict(progress)
    for command in declared:
        results.setdefault(command.id, CheckResult(id=command.id, required=command.required, status='not_run',
            output='private runtime run ended before this result', manifest_digest=digest, context=scope))
    _bound_outputs(results)
    order = {command.id: index for index, command in enumerate(declared)}
    return VerificationReport(execution_source_digest=execution_source_digest, manifest_digest=digest, scope=scope,
                              results=sorted(results.values(), key=lambda result: order.get(result.id, -1)))


async def _scan_bounds(lease: RuntimeLease) -> None:
    stop = threading.Event()
    kwargs = {"stop": stop} if "stop" in inspect.signature(lease.check_bounds).parameters else {}
    worker = asyncio.create_task(asyncio.to_thread(lease.check_bounds, **kwargs))
    try:
        await asyncio.shield(worker)
    finally:
        stop.set()
        with contextlib.suppress(RuntimeScanStoppedError):
            await _settle(worker)


async def run_guarded[T](lease: RuntimeLease, operation: Awaitable[T]) -> T:
    async def watch() -> None:
        while True:
            await _scan_bounds(lease)
            await asyncio.sleep(0.25)

    task = asyncio.ensure_future(operation)
    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait((task, watcher), timeout=max(0, lease.deadline-time.monotonic()),
                                      return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
        if task not in done:
            raise TimeoutError('private runtime execution deadline exhausted')
        return await task
    finally:
        for pending in (task, watcher):
            if not pending.done():
                pending.cancel()
        await _settle(asyncio.gather(task, watcher, return_exceptions=True))
        failure = None if watcher.cancelled() else watcher.exception()
        if failure is not None:
            raise failure


async def execute_with_runtime(
    manifest: EnvironmentManifest, *, cache: str | Path, repo: str | Path, scratch: str | Path,
    install: bool, scope: str, secret_names: Sequence[str], execution_source_digest: str | None,
    runtime_artifacts: RuntimeArtifactReceipt | None, runtime_deadline: float | None,
    runtime_lease: RuntimeLease | None, commands: Callable[..., Awaitable[VerificationReport]],
    redact_values: frozenset[str] = frozenset(),
) -> VerificationReport:
    spec = manifest.runtime
    if spec is None or execution_source_digest is None:
        raise ValueError('private runtime requires accepted manifest and complete source identity')
    kwargs = {"cache": cache, "repo": repo, "scratch": scratch, "install": install, "scope": scope,
              "secret_names": secret_names, "execution_source_digest": execution_source_digest}
    if runtime_lease is not None:
        if (runtime_lease.spec != spec or runtime_lease.manifest_digest != manifest.digest()
                or runtime_lease.source_digest != execution_source_digest):
            raise ValueError('live private runtime identity differs from accepted manifest')
        await _scan_bounds(runtime_lease)
        kwargs["scratch"] = str(runtime_lease.scratch)
        report = await run_guarded(runtime_lease, commands(manifest, **kwargs, runtime_env=runtime_lease.activation_env))
        await _scan_bounds(runtime_lease)
        # The live lease is sealed and stopped by its owner; leftover state must fail here, not at that seal.
        await _reclaim_residual_state(runtime_lease, manifest, report, deadline=runtime_lease.deadline,
                                      secret_names=secret_names, redact_values=redact_values)
        if install:
            runtime_lease.installation_passed = report.passed
        report.runtime_digest = spec.digest()
        report.runtime_phase = 'installer' if runtime_lease.install else 'live_consumer'
        return report
    deadline = runtime_deadline if runtime_deadline is not None else time.monotonic()+spec.policy.resources.max_seconds
    # The caller supplies a scratch base; export only this private project child.
    scratch_base = Path(scratch)
    parent = scratch_base if scratch_base.is_dir() else scratch_base.parent
    with tempfile.TemporaryDirectory(prefix='coordinare-project-', dir=parent) as owned_scratch:
        kwargs['scratch'] = owned_scratch
        lease = await open_runtime(spec, artifacts=runtime_artifacts, repo=Path(repo), cache=Path(cache),
            scratch=Path(owned_scratch), install=install, deadline=deadline,
            manifest_digest=manifest.digest(), source_digest=execution_source_digest)
        report = None
        receipt = runtime_artifacts
        incomplete: list[str] = []
        cause: BaseException | None = None
        progress: dict[str, CheckResult] = {}
        try:
            try:
                report = await run_guarded(lease, commands(manifest, **kwargs, progress=progress,
                                                            runtime_env=lease.activation_env))
                await _scan_bounds(lease)
            except TimeoutError as exc:
                if _cancelling():
                    raise asyncio.CancelledError from exc
                incomplete.append('deadline_exhausted')
                cause = exc
            except (RuntimeError, OSError, ValueError, RuntimeScanStoppedError) as exc:
                # A guest exit, bounds violation or failed scan still holds; keep what the commands did. A pending
                # cancellation always wins: it never becomes a hold.
                if _cancelling():
                    raise asyncio.CancelledError from exc
                incomplete.append('runtime_failed')
                cause = exc
            if incomplete:
                if report is None:
                    report = _partial_report(manifest, progress, install=install, scope=scope,
                                             execution_source_digest=execution_source_digest)
            else:
                await _reclaim_residual_state(lease, manifest, report, deadline=deadline, secret_names=secret_names,
                                              redact_values=redact_values)
                if install and report.passed:
                    lease.installation_passed = True
                    try:
                        receipt = await seal_runtime(lease, manifest_digest=manifest.digest(),
                                                     source_digest=execution_source_digest, deadline=deadline)
                    except (RuntimeError, ValueError, OSError, TimeoutError) as exc:
                        if _cancelling():
                            raise asyncio.CancelledError from exc
                        lease.installation_passed = False
                        incomplete.append('seal_failed')
                        cause = exc
        finally:
            try:
                cleanup = await lease.close(deadline=deadline)
            except (TimeoutError, RuntimeError, OSError) as exc:
                # A failed close is an unconfirmed teardown; it never discards the command evidence, and a pending
                # cancellation still wins.
                if _cancelling():
                    raise asyncio.CancelledError from exc
                cleanup = None
                cause = cause or exc
            if cleanup is not None and not cleanup.clean_shutdown and cause is None and getattr(lease, 'shutdown_error', None):
                # The guest refused or failed its shutdown; close() suppresses that, so without this the hold has no cause.
                cause = RuntimeError(f'private runtime guest did not shut down cleanly: {lease.shutdown_error}')
        teardown = [] if cleanup is not None and cleanup.confirmed and cleanup.clean_shutdown and \
            cleanup.bounds_respected else ([name for name in TEARDOWN_CONDITIONS
                                            if getattr(cleanup, name, True) is False] or ['unconfirmed'])
        if incomplete or teardown:
            report.runtime_digest = spec.digest()
            report.runtime_phase = 'installer' if install else 'consumer'
            report.runtime_cleanup = not teardown
            report.runtime_artifacts = None
            report.runtime_incomplete = [*incomplete, *teardown]
            if cause is not None:
                report.runtime_incomplete_detail = _cause_detail(cause, secret_names, redact_values)
            raise PrivateRuntimeIncompleteError(report.runtime_incomplete, report)
        report.runtime_digest = spec.digest()
        report.runtime_phase = 'installer' if install else 'consumer'
        report.runtime_artifacts = receipt
        report.runtime_cleanup = True
        return report

"""Per-repository guest choice from the pinned source alone: native unless a dev/test requirement names the other CPU.

Explicit means a literal platform in a build, run, pull or compose invocation: ``--platform linux/amd64`` (or ``=``),
``platform: linux/amd64`` in YAML, or ``DOCKER_DEFAULT_PLATFORM``. A variable, a comment or a non-Linux platform never
counts. In GitHub workflows the decision is per step: a publish-only step (a push to a registry) is excluded and named,
an unclassifiable step counts and is named. No model reads or weighs these files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from .execution_source import ExecutionSourceError, bundle_texts

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from .execution_source import ExecutionSourceIdentity

ARCHITECTURES = {'amd64': 'linux/x86_64', 'x86_64': 'linux/x86_64', 'arm64': 'linux/aarch64', 'aarch64': 'linux/aarch64'}
MAX_TRIGGERS = 32
MAX_FILE_BYTES = 256 * 1024
_PLATFORM = r'[A-Za-z0-9_.:/-]+'
_FLAG = re.compile(r'--platform(?:=|\s+)(' + _PLATFORM + r'(?:,' + _PLATFORM + r')*)')
_KEY = re.compile(r'^\s*(?:-\s+)?platforms?\s*:\s*["\']?(' + _PLATFORM + r'(?:,' + _PLATFORM + r')*)["\']?\s*$', re.MULTILINE)
_DEFAULT = re.compile(r'DOCKER_DEFAULT_PLATFORM\s*[:=]\s*["\']?(' + _PLATFORM + r')')
_COMPOSE = re.compile(r'(?:docker-)?compose[^/]*\.ya?ml')


@dataclass(frozen=True)
class GuestSelection:
    """``required`` holds the explicit Linux platforms found; ``triggers`` the paths that named them (paths only)."""

    required: tuple[str, ...]
    triggers: tuple[str, ...]
    unsupported: tuple[str, ...]
    excluded: tuple[str, ...] = ()
    unclassified: tuple[str, ...] = ()


def candidate(path: str) -> bool:
    """Build, compose, CI and script files: where a repository names the platform it builds or runs on."""
    name = path.rsplit('/', 1)[-1]
    if name.startswith('Dockerfile') or name.endswith('.Dockerfile') or _COMPOSE.fullmatch(name):
        return True
    if path.startswith(('.github/workflows/', '.circleci/')) or name == '.gitlab-ci.yml':
        return name.endswith(('.yml', '.yaml'))
    if name in {'Makefile', 'makefile', 'GNUmakefile'} or name.endswith('.mk'):
        return True
    return name.endswith(('.sh', '.bash')) or path.startswith('bin/') or '/bin/' in path


def _platforms(values: str) -> list[str]:
    return [value for value in values.split(',') if value]


def _literals(path: str, text: str) -> list[str]:
    text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
    found = [value for match in _FLAG.finditer(text) for value in _platforms(match.group(1))]
    found += [value for match in _DEFAULT.finditer(text) for value in _platforms(match.group(1))]
    if path.endswith(('.yml', '.yaml')):
        found += [value for match in _KEY.finditer(text) for value in _platforms(match.group(1))]
    return found


_PUBLISH = re.compile(r'--push\b|--output[= ]+type=registry|\bdocker (?:manifest )?push\b')
_BUILD_PUSH = 'docker/build-push-action'


def _shell(step: dict[str, Any]) -> str:
    """The commands a step runs: its run script, or the command or script an action such as retry is given."""
    with_ = step.get('with') if isinstance(step.get('with'), dict) else {}
    return '\n'.join(part for part in (step.get('run'), with_.get('command'), with_.get('script')) if isinstance(part, str))


def _publishes(step: dict[str, Any]) -> bool | None:
    """True for a publish-only step, False for one that does not publish, None when the step cannot be classified."""
    uses = step.get('uses')
    if isinstance(uses, str) and uses.startswith(_BUILD_PUSH):
        push = (step.get('with') if isinstance(step.get('with'), dict) else {}).get('push')
        if push is None or push is False or push == 'false':
            return False
        return True if push is True or push == 'true' else None
    return bool(_PUBLISH.search(_shell(step)))


def _named(value: str) -> bool:
    return value.startswith('linux/') and value.split('/', 1)[1] in ARCHITECTURES


def _dump(value: Any) -> str:
    return yaml.safe_dump(value, sort_keys=True)


def scan_units(path: str, text: str) -> tuple[list[str], list[str], list[str]]:
    """Texts to scan for platforms, the excluded publish steps, and the unclassified CI units (each counted)."""
    if not path.startswith(('.github/workflows/',)):
        return [text], [], []
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        document = None
    jobs = document.get('jobs') if isinstance(document, dict) else None
    if not isinstance(jobs, dict):
        return [text], [], [path]
    units = [_dump({key: value for key, value in document.items() if key != 'jobs'})]
    excluded: list[str] = []
    unclassified: list[str] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            unclassified.append(f'{path} {job_id}')
            units.append(_dump(job))
            continue
        units.append(_dump({key: value for key, value in job.items() if key != 'steps'}))
        steps = job.get('steps') if isinstance(job.get('steps'), list) else []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                unclassified.append(f'{path} {job_id}/{index}')
                units.append(_dump(step))
                continue
            label = f'{path} {job_id}/{step["name"] if isinstance(step.get("name"), str) else index}'
            unit = _shell(step) + '\n' + _dump({key: value for key, value in step.items() if key != 'run'})
            verdict = _publishes(step)
            if verdict is None:
                unclassified.append(label)
            if verdict:
                # Only a publish step that names a platform is decision-relevant, so only it is recorded.
                if any(_named(value) for value in _literals(path, unit)):
                    excluded.append(label)
                continue
            units.append(unit)
    return units, excluded[:MAX_TRIGGERS], unclassified[:MAX_TRIGGERS]


def select(files: Iterable[tuple[str, str]]) -> GuestSelection:
    """Explicit Linux platforms named by the given (path, pinned text) pairs; triggers are the files naming one."""
    required: set[str] = set()
    triggers: set[str] = set()
    unsupported: set[str] = set()
    excluded: list[str] = []
    unclassified: list[str] = []
    for path, text in sorted(files):
        if not candidate(path):
            continue
        units, skipped, unknown = scan_units(path, text)
        excluded += skipped
        unclassified += unknown
        for unit in units:
            for value in _literals(path, unit):
                if value.startswith('linux/') and value.split('/', 1)[1] in ARCHITECTURES:
                    required.add(ARCHITECTURES[value.split('/', 1)[1]])
                    triggers.add(path)
                elif value.startswith('linux/') or '/' in value:
                    unsupported.add(value[:64])
    return GuestSelection(required=tuple(sorted(required)), triggers=tuple(sorted(triggers))[:MAX_TRIGGERS],
                          unsupported=tuple(sorted(unsupported))[:MAX_TRIGGERS], excluded=tuple(excluded[:MAX_TRIGGERS]),
                          unclassified=tuple(unclassified[:MAX_TRIGGERS]))


def pinned_requirements(bundle: Path, paths: Iterable[str], *, expected: ExecutionSourceIdentity) -> dict[str, list[str]]:
    """The durable record of a selection, from the packed pinned source the daemon stage holds.

    Candidate files are read from the archive itself (exact pinned bytes, no network). A selection never scans less than
    it was given: candidates that exist but cannot be read, or exceed the bound, hold the attempt with a named code.
    """
    names = sorted(path for path in paths if candidate(path))
    texts, oversize = bundle_texts(bundle, names, expected=expected, limit=MAX_FILE_BYTES)
    if oversize or (names and not texts):
        message = 'guest selection could not read the pinned build files'
        raise ExecutionSourceError(message, code='guest_selection_unreadable')
    selection = select(texts.items())
    return {'required': list(selection.required), 'triggers': list(selection.triggers),
            'unsupported': list(selection.unsupported), 'excluded_publish_steps': list(selection.excluded),
            'unclassified_ci': list(selection.unclassified)}


def guest_for(carrier: str, required: Iterable[str]) -> str:
    """The other CPU's guest when an explicit requirement names it; otherwise the carrier's own guest."""
    foreign = [platform for platform in required if platform != carrier]
    return foreign[0] if foreign else carrier

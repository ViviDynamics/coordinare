"""Per-repository guest choice from the pinned source alone: native unless an explicit requirement names the other CPU.

Explicit means a literal platform in a build, run, pull or compose invocation: ``--platform linux/amd64`` (or ``=``),
``platform: linux/amd64`` in YAML, or ``DOCKER_DEFAULT_PLATFORM``. A variable, a comment or a non-Linux platform never
counts. No model reads or weighs these files; the caller supplies only candidate paths and their pinned text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

ARCHITECTURES = {'amd64': 'linux/x86_64', 'x86_64': 'linux/x86_64', 'arm64': 'linux/aarch64', 'aarch64': 'linux/aarch64'}
MAX_TRIGGERS = 32
MAX_FILE_BYTES = 256 * 1024
_PLATFORM = r'[A-Za-z0-9_.:/-]+'
_FLAG = re.compile(r'--platform(?:=|\s+)(' + _PLATFORM + r'(?:,' + _PLATFORM + r')*)')
_KEY = re.compile(r'^\s*(?:-\s+)?platform\s*:\s*["\']?(' + _PLATFORM + r'(?:,' + _PLATFORM + r')*)["\']?\s*$', re.MULTILINE)
_DEFAULT = re.compile(r'DOCKER_DEFAULT_PLATFORM\s*[:=]\s*["\']?(' + _PLATFORM + r')')
_COMPOSE = re.compile(r'(?:docker-)?compose[^/]*\.ya?ml')


@dataclass(frozen=True)
class GuestSelection:
    """``required`` holds the explicit Linux platforms found; ``triggers`` the paths that named them (paths only)."""

    required: tuple[str, ...]
    triggers: tuple[str, ...]
    unsupported: tuple[str, ...]


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


def select(files: Iterable[tuple[str, str]]) -> GuestSelection:
    """Explicit Linux platforms named by the given (path, pinned text) pairs; triggers are the files naming one."""
    required: set[str] = set()
    triggers: set[str] = set()
    unsupported: set[str] = set()
    for path, text in sorted(files):
        if not candidate(path):
            continue
        for value in _literals(path, text):
            if value.startswith('linux/') and value.split('/', 1)[1] in ARCHITECTURES:
                required.add(ARCHITECTURES[value.split('/', 1)[1]])
                triggers.add(path)
            elif value.startswith('linux/') or '/' in value:
                unsupported.add(value[:64])
    return GuestSelection(required=tuple(sorted(required)), triggers=tuple(sorted(triggers))[:MAX_TRIGGERS],
                          unsupported=tuple(sorted(unsupported))[:MAX_TRIGGERS])


def read_candidates(root: Path, paths: Iterable[str]) -> list[tuple[str, str]]:
    """Candidate files of the pinned tree, as text; symlinks, special files and oversize files are never read."""
    found: list[tuple[str, str]] = []
    for path in sorted(paths):
        if not candidate(path):
            continue
        target = root / path
        if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
            continue
        found.append((path, target.read_text(encoding='utf-8', errors='replace')))
    return found


def pinned_requirements(root: Path, paths: Iterable[str]) -> dict[str, list[str]]:
    """The durable record of a selection: platforms, the paths that named them, and unsupported literals."""
    selection = select(read_candidates(root, paths))
    return {'required': list(selection.required), 'triggers': list(selection.triggers),
            'unsupported': list(selection.unsupported)}


def guest_for(carrier: str, required: Iterable[str]) -> str:
    """The other CPU's guest when an explicit requirement names it; otherwise the carrier's own guest."""
    foreign = [platform for platform in required if platform != carrier]
    return foreign[0] if foreign else carrier

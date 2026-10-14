"""Write scope for read-mostly roles (issue #98).

A role whose output is a report, not a change, still runs as a free agent turn
with write access to its clone. Nothing stopped such a turn from editing a
source or test file it had read, and the next commit carried the edit out.

This module is the harness-side check. It lists what a turn dirtied, discards
whatever falls outside the role's allowlist, and returns those paths so the
verdict can name them. Allowlists are path-exact, not prefixes: a role gets
the files it names and nothing beside them.

Untracked build detritus (``__pycache__`` and the like) is not a change to the
repository and is left alone. A tracked file is always in scope.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import structlog

from performer.models import Stand
from performer.noise_paths import is_sanitizer_omitted_path
from performer.workspace import WorkspaceSetupError, _run_git, _run_git_stdout

log = structlog.get_logger(__name__)

#: Cap on paths carried in a log line or a verdict. Every out-of-scope path is
#: still discarded; only the report of them is bounded.
MAX_REPORTED_PATHS = 20

_UNTRACKED = "??"
_RENAME_OR_COPY = frozenset("RC")


@dataclass(frozen=True)
class DirtyPath:
    """One entry of ``git status --porcelain``: the two status letters and a path."""

    status: str
    path: str


def _git_env(stand: Stand) -> dict[str, str]:
    return {**os.environ, **stand.git_env} if stand.git_env else {**os.environ}


def parse_porcelain_z(output: str) -> list[DirtyPath]:
    """Parse ``git status --porcelain -z``.

    Entries are NUL-terminated ``XY path``. A rename or copy is followed by its
    source path as a separate entry, so the source is reported too.
    """
    tokens = [token for token in output.split("\0") if token]
    entries: list[DirtyPath] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        status, path = token[:2], token[3:]
        entries.append(DirtyPath(status, path))
        if _RENAME_OR_COPY & set(status) and index < len(tokens):
            entries.append(DirtyPath(status, tokens[index]))
            index += 1
    return entries


async def dirty_paths(stand: Stand) -> list[DirtyPath]:
    """Every changed or untracked path in the clone. Ignored paths do not appear."""
    rc, out = await _run_git_stdout(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        stand.path,
        _git_env(stand),
    )
    if rc != 0:
        message = f"git status failed (exit {rc}); the write scope could not be checked"
        raise WorkspaceSetupError(message)
    return parse_porcelain_z(out)


async def head_paths(stand: Stand) -> set[str]:
    """Every path tracked at HEAD."""
    rc, out = await _run_git_stdout(
        ["git", "ls-tree", "-r", "-z", "--name-only", "HEAD"],
        stand.path,
        _git_env(stand),
    )
    if rc != 0:
        message = f"git ls-tree failed (exit {rc}); the write scope could not be checked"
        raise WorkspaceSetupError(message)
    return {path for path in out.split("\0") if path}


def _out_of_scope(entries: list[DirtyPath], allowed: set[str]) -> list[str]:
    outside: list[str] = []
    for entry in entries:
        if entry.status == _UNTRACKED and is_sanitizer_omitted_path(entry.path):
            continue
        if entry.path in allowed or entry.path in outside:
            continue
        outside.append(entry.path)
    return outside


def _is_safe_relative(path: str) -> bool:
    return bool(path) and not Path(path).is_absolute() and ".." not in Path(path).parts


async def _discard(stand: Stand, paths: list[str]) -> None:
    """Restore tracked paths from HEAD and remove the rest.

    Raises WorkspaceSetupError when git refuses; a failed discard must not be
    followed by a publish.
    """
    env = _git_env(stand)
    tracked = await head_paths(stand)
    restore = [path for path in paths if path in tracked]
    stray = [path for path in paths if path not in tracked]
    if restore:
        rc, err = await _run_git(
            ["git", "checkout", "HEAD", "--", *restore],
            cwd=stand.path,
            env=env,
        )
        if rc != 0:
            message = f"could not restore {len(restore)} path(s) from HEAD (exit {rc}): {err[:200]}"
            raise WorkspaceSetupError(message)
    if stray:
        rc, err = await _run_git(
            ["git", "rm", "-q", "-f", "--cached", "--ignore-unmatch", "--", *stray],
            cwd=stand.path,
            env=env,
        )
        if rc != 0:
            message = f"could not unstage {len(stray)} path(s) (exit {rc}): {err[:200]}"
            raise WorkspaceSetupError(message)
        for path in stray:
            target = stand.path / path
            if _is_safe_relative(path) and (target.is_file() or target.is_symlink()):
                target.unlink()


async def enforce_write_scope(stand: Stand, allowed: set[str], *, role: str) -> list[str]:
    """Discard every changed path outside *allowed* and return those paths.

    The returned list is in git's order, with duplicates removed. It is empty
    when the turn stayed in scope. Raises WorkspaceSetupError when the tree
    cannot be read or restored, so an unverified write is never published.
    """
    outside = _out_of_scope(await dirty_paths(stand), allowed)
    if outside:
        await _discard(stand, outside)
        log.warning(
            "write_scope.discarded",
            role=role,
            count=len(outside),
            paths=outside[:MAX_REPORTED_PATHS],
        )
    return outside

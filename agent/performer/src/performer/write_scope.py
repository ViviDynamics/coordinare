"""Write scope for read-mostly roles (issue #98).

A role whose output is a report, not a change, still runs as a free agent turn
with write access to its clone. Nothing stopped such a turn from editing a
source or test file it had read, and the next commit carried the edit out.

This module is the harness-side check. It lists what a turn changed and sorts
every path outside the role's allowlist into two groups:

* ``restored``: tracked at HEAD and changed. The file is restored from HEAD.
  This is a verdict problem for the caller.
* ``left_out``: untracked, or staged but not tracked. Staged entries are
  unstaged, nothing is deleted, and nothing outside the allowlist is ever
  committed. This is not a verdict problem: test runs routinely leave coverage
  output and build artifacts behind.

Allowlists are path-exact, not prefixes: a role gets the files it names and
nothing beside them. Untracked build detritus (``__pycache__`` and the like) is
not a change to the repository and is not reported at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import structlog

from performer.models import Stand
from performer.noise_paths import is_sanitizer_omitted_path
from performer.workspace import WorkspaceSetupError, _run_git, _run_git_stdout

log = structlog.get_logger(__name__)

#: Cap on paths carried in a log line or a verdict. The full set is still
#: handled; only the report of it is bounded.
MAX_REPORTED_PATHS = 20

_UNTRACKED = "??"
_RENAME_OR_COPY = frozenset("RC")
_UNSTAGED_STATUS = frozenset(" ?")


@dataclass(frozen=True)
class DirtyPath:
    """One entry of ``git status --porcelain``: the two status letters and a path."""

    status: str
    path: str


@dataclass(frozen=True)
class ScopeResult:
    """What the guard did with the paths outside the allowlist."""

    restored: list[str]
    left_out: list[str]


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


async def _restore_from_head(stand: Stand, paths: list[str]) -> None:
    rc, err = await _run_git(
        ["git", "checkout", "HEAD", "--", *paths],
        cwd=stand.path,
        env=_git_env(stand),
    )
    if rc != 0:
        message = f"could not restore {len(paths)} path(s) from HEAD (exit {rc}): {err[:200]}"
        raise WorkspaceSetupError(message)


async def _unstage(stand: Stand, paths: list[str]) -> None:
    """Remove *paths* from the index only. The files stay on disk."""
    rc, err = await _run_git(
        ["git", "rm", "-q", "-f", "--cached", "--ignore-unmatch", "--", *paths],
        cwd=stand.path,
        env=_git_env(stand),
    )
    if rc != 0:
        message = f"could not unstage {len(paths)} path(s) (exit {rc}): {err[:200]}"
        raise WorkspaceSetupError(message)


async def enforce_write_scope(stand: Stand, allowed: set[str], *, role: str) -> ScopeResult:
    """Sort every changed path outside *allowed* and act on it.

    Tracked paths are restored from HEAD. Untracked and staged-only paths are
    unstaged (never deleted) and reported as left out. Nothing here commits.
    Raises WorkspaceSetupError when the tree cannot be read or restored, so an
    unverified write is never published.
    """
    entries = await dirty_paths(stand)
    outside = _out_of_scope(entries, allowed)
    if not outside:
        return ScopeResult(restored=[], left_out=[])

    tracked = await head_paths(stand)
    restored = [path for path in outside if path in tracked]
    left_out = [path for path in outside if path not in tracked]
    staged_set = {entry.path for entry in entries if entry.status[:1] not in _UNSTAGED_STATUS}
    staged = [path for path in left_out if path in staged_set]

    if restored:
        await _restore_from_head(stand, restored)
        log.warning(
            "write_scope.restored",
            role=role,
            count=len(restored),
            paths=restored[:MAX_REPORTED_PATHS],
        )
    if staged:
        await _unstage(stand, staged)
    if left_out:
        log.info(
            "write_scope.left_out",
            role=role,
            count=len(left_out),
            staged_unstaged=len(staged),
            paths=left_out[:MAX_REPORTED_PATHS],
        )
    return ScopeResult(restored=restored, left_out=left_out)

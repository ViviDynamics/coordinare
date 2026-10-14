"""Write scope for read-mostly roles (issue #98).

A role whose output is a report, not a change, still runs as a free agent turn
with write access to its clone. Nothing stopped such a turn from editing a
source or test file it had read, and the next commit carried the edit out.

This module is the harness-side check. It has three jobs.

1. Commits the agent made during the turn are checked against the turn's
   starting commit. A commit that touches a path outside the allowlist is
   removed from the branch: the branch is rewound and the out-of-scope files are
   restored from the starting commit. That is a verdict problem.
2. Tracked files the turn changed in the working tree, and did not declare, are
   restored from HEAD and reported as information. They were never published
   (the commit step only commits declared paths), so they do not fail a verdict.
   Untracked files are never committed, are left on disk, and are information.
3. A declared new-file path is checked by :func:`declared_path_refusal` before
   anything is written. A refusal names the path and the reason, and fails the
   verdict.

Allowlists are path-exact, not prefixes. Every git call runs with
``GIT_LITERAL_PATHSPECS=1``, so a path is never a pattern.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

import structlog

from performer.models import Stand
from performer.noise_paths import is_sanitizer_omitted_path
from performer.workspace import WorkspaceSetupError, _run_git, _run_git_stdout

log = structlog.get_logger(__name__)

#: Cap on paths carried in a log line or a verdict. The full set is still handled;
#: only the report of it is bounded.
MAX_REPORTED_PATHS = 20

#: Source extensions a new test file may use. ``.bats`` is named by its own extension.
TEST_SOURCE_EXTENSIONS = frozenset(
    {
        "py",
        "js",
        "jsx",
        "ts",
        "tsx",
        "mjs",
        "cjs",
        "go",
        "rs",
        "rb",
        "java",
        "kt",
        "cs",
        "php",
        "swift",
        "ex",
        "exs",
    },
)
BATS_EXTENSION = "bats"

_CONFIG_NAMES = frozenset({"conftest.py", "__init__.py", "package.json", "makefile", "dockerfile"})
_CONFIG_SUFFIXES = (".ini", ".cfg", ".toml", ".json", ".yml", ".yaml")
_CONFIG_PREFIXES = ("setup.", "jest.config.", "vitest.config.")
_HOOK_DIR = "hooks"
_MAGIC_CHARACTERS = frozenset("*?[")
_CONTROL_CHARACTERS = frozenset("\0\n\r\t")
#: A test file's stem is ``test_*``, ``*_test``, ``*_spec``, ``*.test`` or ``*.spec``.
_TEST_STEM = re.compile(r"^test_|^test$|^spec$|_test$|_spec$|\.test$|\.spec$")

_UNTRACKED = "??"
_RENAME_OR_COPY = frozenset("RC")
_UNSTAGED_STATUS = frozenset(" ?")


@dataclass(frozen=True)
class DirtyPath:
    """One entry of ``git status --porcelain``: the two status letters and a path."""

    status: str
    path: str


@dataclass(frozen=True)
class Refusal:
    """A declared path the turn may not write, and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class ScopeReport:
    """What the guard found and did.

    ``committed_out_of_scope`` fails the verdict: the turn committed these paths,
    and the commits were removed. ``restored`` and ``left_out`` are information.
    """

    committed_out_of_scope: list[str]
    restored: list[str]
    left_out: list[str]
    reset_to: str | None


def _git_env(stand: Stand) -> dict[str, str]:
    env = {**os.environ, **stand.git_env} if stand.git_env else {**os.environ}
    env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


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


async def head_paths(stand: Stand, rev: str = "HEAD") -> set[str]:
    """Every path tracked at *rev* (HEAD by default)."""
    rc, out = await _run_git_stdout(
        ["git", "ls-tree", "-r", "-z", "--name-only", rev],
        stand.path,
        _git_env(stand),
    )
    if rc != 0:
        message = f"git ls-tree failed (exit {rc}); the write scope could not be checked"
        raise WorkspaceSetupError(message)
    return {path for path in out.split("\0") if path}


async def _rev_parse(stand: Stand, rev: str) -> str:
    rc, out = await _run_git_stdout(
        ["git", "rev-parse", "--verify", rev], stand.path, _git_env(stand)
    )
    if rc != 0:
        message = f"git rev-parse {rev} failed (exit {rc}); the write scope could not be checked"
        raise WorkspaceSetupError(message)
    return out.strip()


async def _is_ancestor(stand: Stand, ancestor: str, descendant: str) -> bool:
    rc, err = await _run_git(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=stand.path,
        env=_git_env(stand),
    )
    if rc == 0:
        return True
    if rc == 1:
        return False
    message = f"git merge-base failed (exit {rc}): {err[:200]}"
    raise WorkspaceSetupError(message)


async def _changed_between(stand: Stand, base: str, head: str) -> list[str]:
    rc, out = await _run_git_stdout(
        ["git", "diff", "--name-only", "-z", "--no-renames", base, head],
        stand.path,
        _git_env(stand),
    )
    if rc != 0:
        message = (
            f"git diff {base}..{head} failed (exit {rc}); the write scope could not be checked"
        )
        raise WorkspaceSetupError(message)
    return [path for path in out.split("\0") if path]


async def _reset_mixed(stand: Stand, rev: str) -> None:
    """Move the branch to *rev*. The working tree is kept; the index follows the branch."""
    rc, err = await _run_git(
        ["git", "reset", "-q", "--mixed", rev],
        cwd=stand.path,
        env=_git_env(stand),
    )
    if rc != 0:
        message = f"could not rewind the branch to {rev[:12]} (exit {rc}): {err[:200]}"
        raise WorkspaceSetupError(message)


async def _restore_from(stand: Stand, rev: str, paths: list[str]) -> None:
    rc, err = await _run_git(
        ["git", "checkout", rev, "--", *paths],
        cwd=stand.path,
        env=_git_env(stand),
    )
    if rc != 0:
        message = f"could not restore {len(paths)} path(s) from {rev[:12]} (exit {rc}): {err[:200]}"
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


def _unique(paths: list[str]) -> list[str]:
    return list(dict.fromkeys(paths))


def _out_of_scope(entries: list[DirtyPath], allowed: set[str]) -> list[str]:
    outside: list[str] = []
    for entry in entries:
        if entry.status == _UNTRACKED and is_sanitizer_omitted_path(entry.path):
            continue
        if entry.path in allowed or entry.path in outside:
            continue
        outside.append(entry.path)
    return outside


async def enforce_write_scope(
    stand: Stand,
    allowed: set[str],
    *,
    role: str,
    head_at_start: str | None,
) -> ScopeReport:
    """Check the turn's commits, then its working tree, against *allowed*.

    *head_at_start* is the commit the turn began from. Without it, or when HEAD
    no longer descends from it, the turn's writes cannot be verified and the call
    raises WorkspaceSetupError: nothing is published.
    Raises WorkspaceSetupError too when git cannot read or restore the tree.
    """
    if not head_at_start:
        message = "the turn's starting commit is unknown; the write scope cannot be verified"
        raise WorkspaceSetupError(message)
    head_now = await _rev_parse(stand, "HEAD")

    committed_out: list[str] = []
    reset_to: str | None = None
    if head_now != head_at_start:
        if not await _is_ancestor(stand, head_at_start, head_now):
            message = "HEAD no longer descends from the turn's starting commit; the write scope cannot be verified"
            raise WorkspaceSetupError(message)
        made = await _changed_between(stand, head_at_start, head_now)
        committed_out = _unique([path for path in made if path not in allowed])
        if committed_out:
            await _reset_mixed(stand, head_at_start)
            reset_to = head_at_start
            start_tree = await head_paths(stand, head_at_start)
            back = [path for path in committed_out if path in start_tree]
            if back:
                await _restore_from(stand, head_at_start, back)
            log.warning(
                "write_scope.committed_out_of_scope",
                role=role,
                count=len(committed_out),
                paths=committed_out[:MAX_REPORTED_PATHS],
                reset_to=head_at_start[:12],
            )

    entries = await dirty_paths(stand)
    outside = _out_of_scope(entries, allowed)
    tracked = await head_paths(stand)
    restored = [path for path in outside if path in tracked]
    left_out = [path for path in outside if path not in tracked]
    staged_set = {entry.path for entry in entries if entry.status[:1] not in _UNSTAGED_STATUS}
    staged = [path for path in left_out if path in staged_set]

    if restored:
        await _restore_from(stand, "HEAD", restored)
        log.info(
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
    return ScopeReport(
        committed_out_of_scope=committed_out,
        restored=restored,
        left_out=left_out,
        reset_to=reset_to,
    )


def _filesystem_refusal(root: Path, parts: list[str]) -> str | None:
    """Refuse a path whose ancestor or final component is a symlink, or whose final
    component is a directory. Every component is checked with lstat, so nothing is
    followed out of the clone. A missing component ends the walk: nothing below it exists."""
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return "path crosses a symlink (an ancestor or the path itself)"
        if index == len(parts) - 1 and stat.S_ISDIR(info.st_mode):
            return "path is a directory, not a file"
    return None


def declared_path_refusal(path: object, *, root: Path, tracked: set[str]) -> str | None:
    """Why a QA turn may not create *path*, or None when it may.

    The checks run in order and stop at the first failure, so the reason names one
    cause. *tracked* is the set of paths at the turn's starting commit. Nothing is
    written here: a refused path is never touched.
    """
    if not isinstance(path, str) or not path:
        return "not a path"
    parts = path.split("/")
    return (
        _text_refusal(path)
        or _location_refusal(parts)
        or _tracked_refusal(path, tracked)
        or _config_refusal(parts)
        or _filesystem_refusal(root, parts)
        or _test_file_refusal(parts[-1])
    )


def _text_refusal(path: str) -> str | None:
    if set(path) & _CONTROL_CHARACTERS:
        return "contains control characters"
    if "\\" in path:
        return "contains a backslash"
    if (set(path) & _MAGIC_CHARACTERS) or path.startswith(":"):
        return "contains glob or magic pathspec characters"
    return None


def _location_refusal(parts: list[str]) -> str | None:
    if parts[0] == "":
        return "absolute path"
    if any(part in ("", ".", "..") for part in parts):
        return "traversing or non-normalised path"
    if any(part.startswith(".") for part in parts):
        return "dot-directory or dot-file component"
    return None


def _tracked_refusal(path: str, tracked: set[str]) -> str | None:
    if path in tracked:
        return "exists at HEAD; existing files are never overwritten"
    if path.casefold() in {tracked_path.casefold() for tracked_path in tracked}:
        return "case collision with a tracked path"
    return None


def _config_refusal(parts: list[str]) -> str | None:
    name = parts[-1].lower()
    if (
        name in _CONFIG_NAMES
        or name.endswith(_CONFIG_SUFFIXES)
        or name.startswith(_CONFIG_PREFIXES)
        or any(part.lower() == _HOOK_DIR for part in parts)
    ):
        return "configuration or hook file"
    return None


def _test_file_refusal(name: str) -> str | None:
    """The name must be a test source file by extension and by naming rule."""
    lower = name.lower()
    extension = lower.rsplit(".", 1)[1] if "." in lower else ""
    if extension == BATS_EXTENSION:
        return None
    if extension not in TEST_SOURCE_EXTENSIONS:
        return "extension is not a test source extension"
    stem = lower[: -(len(extension) + 1)]
    if not _TEST_STEM.search(stem):
        return "not named as a test (test_*, *_test, *_spec, *.test, *.spec)"
    return None

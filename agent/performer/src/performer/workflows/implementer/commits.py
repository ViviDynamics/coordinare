"""Git helpers for commit squashing and scope reversion (spec 167).

Pure async functions that manipulate git state: getting SHAs, tracking changes,
squashing turns into step commits, reverting out-of-scope edits.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import structlog

from performer import degeneracy
from performer.degeneracy import format_refusal

log = structlog.get_logger(__name__)

_GIT = shutil.which("git") or "git"  # 440: resolve the real git path once
_READ_CHUNK = 1 << 20

__all__ = [
    "head_sha",
    "changed_paths_since",
    "squash_turn_commits",
    "revert_paths",
    "commit_paths",
    "branch_commit_entries",
    "staged_paths",
    "staged_blob_prefix",
    "classify_staged_blob",
    "unstage_paths",
    "unstage_degenerate_staged",
]


async def staged_paths(workspace: Path) -> list[str]:
    """Paths currently staged (index vs HEAD), from ``git diff --cached`` (#396).

    Empty on any git failure: the seams that consume this list fail open to
    their own content checks rather than blocking on a probe error.
    """
    rc, stdout = await _run_git_stdout(
        [_GIT, "diff", "--cached", "--name-only", "-z"],
        workspace,
    )
    if rc != 0:
        return []
    return [p for p in stdout.split("\0") if p]


async def unstage_paths(workspace: Path, paths: list[str]) -> None:
    """Reset index entries for *paths* to HEAD, best-effort (#396).

    ``git commit -m`` commits the whole index, so content staged earlier in
    the turn would ride along with any later commit. Resetting the entry
    removes it from the index; the worktree file, if any, is left for the
    normal revert/cleanup paths.
    """
    if not paths:
        return
    await _run_git([_GIT, "reset", "-q", "--"] + paths, workspace)


async def staged_blob_prefix(
    workspace: Path, path: str, limit: int,
) -> tuple[int, bytes] | None:
    """The staged blob's true size plus at most *limit* leading bytes (#396).

    The stream is cut off after *limit* bytes: a multi-gigabyte staged blob
    is never buffered whole. None on any failure (path not in the index, git
    error): callers fail open to their own content checks rather than
    blocking on a probe error.
    """
    rc, stdout = await _run_git_stdout(
        [_GIT, "cat-file", "-s", f":{path}"],
        workspace,
    )
    if rc != 0:
        return None
    try:
        size = int(stdout.strip())
    except ValueError:
        return None

    proc = await asyncio.create_subprocess_exec(
        _GIT, "show", f":{path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(workspace),
    )
    chunks: list[bytes] = []
    remaining = limit + 1
    try:
        while remaining > 0:
            chunk = await proc.stdout.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        try:
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        # wait() alone can deadlock: a killed child's stdout pipe may still
        # hold unread bytes, and the transport never drains them. communicate
        # drains stdout to EOF (the killed child closes the pipe) first.
        await proc.communicate()
    return size, b"".join(chunks)[:limit]


async def classify_staged_blob(
    workspace: Path, path: str, **kwargs: object,
) -> "degeneracy.DegeneracyVerdict | None":
    """Stream-classify the blob staged at *path* in full (#396).

    The blob is streamed from ``git show`` through the bounded incremental
    scanner, so a multi-gigabyte staged blob is classified whole — prefix
    and tail — without being buffered. Every streamed chunk is sniffed for
    NUL, so a binary asset whose early bytes are text-like is still
    skipped; the size rule, when enabled, then rejects an oversized text
    blob from its true ``cat-file -s`` size. None on any probe failure:
    callers fail open to their own content checks rather than blocking on a
    probe error.
    """
    rc, stdout = await _run_git_stdout(
        [_GIT, "cat-file", "-s", f":{path}"],
        workspace,
    )
    if rc != 0:
        return None
    try:
        size = int(stdout.strip())
    except ValueError:
        return None

    proc = await asyncio.create_subprocess_exec(
        _GIT, "show", f":{path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(workspace),
    )
    if proc.stdout is None:
        return None
    cap = kwargs.get("size_cap") if "size_cap" in kwargs else degeneracy.DEFAULT_SIZE_CAP
    scanner = degeneracy.stream_scanner(**kwargs)
    verdict: "degeneracy.DegeneracyVerdict | None" = None
    try:
        while True:
            chunk = await proc.stdout.read(_READ_CHUNK)
            if not chunk:
                break
            if b"\x00" in chunk:
                # Binary anywhere in the blob: skipped, not classified as a
                # degenerate text artifact. The scanner is memory-bounded, so
                # the whole blob is streamed before the size verdict.
                return degeneracy.DegeneracyVerdict(degenerate=False)
            scanner.feed(chunk)
        if cap is not None and size > int(cap):
            return degeneracy.DegeneracyVerdict(
                degenerate=True,
                reasons=(degeneracy._size_reason(size, int(cap)),),
            )
        verdict = scanner.finish()
    finally:
        try:
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        # wait() alone can deadlock: a killed child's stdout pipe may still
        # hold unread bytes, and the transport never drains them. communicate
        # drains stdout to EOF (the killed child closes the pipe) first.
        await proc.communicate()
    return verdict


async def unstage_degenerate_staged(
    workspace: Path, keep: dict[str, str] | set[str],
) -> list[dict[str, str]]:
    """Unstage any degenerate blob in the index that *keep* does not cover.

    ``commit_paths`` stages its paths and then commits the whole index, so a
    degenerate blob staged but absent from the change set (agent state, a
    parser miss) would ride a healthy commit. Degenerate leftovers are
    unstaged — the worktree copy, if any, is left for the normal cleanup
    paths — and one entry per unstaged path is returned. Fail-open: a probe
    error (missing workspace, git failure) leaves the index untouched.
    """
    removed: list[dict[str, str]] = []
    try:
        staged = await staged_paths(workspace)
        suspects = [p for p in staged if p not in keep]
        for path in suspects:
            verdict = await classify_staged_blob(workspace, path, size_cap=None)
            if verdict is None or not verdict.degenerate:
                continue
            await unstage_paths(workspace, [path])
            removed.append(
                {
                    "path": path,
                    "kind": "unstaged_degenerate",
                    "reason": format_refusal(path, verdict),
                }
            )
    except OSError:
        return removed
    return removed


async def _run_git(
    args: list[str],
    cwd: Path,
    timeout: float = 120.0,
) -> tuple[int, str]:
    """Run a git command and return (returncode, stderr)."""
    env = {**os.environ}
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
    )
    try:
        _stdout, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"git command timed out after {timeout}s: {' '.join(args)}")
    return proc.returncode, stderr_bytes.decode(errors="replace")


async def _run_git_stdout(
    args: list[str],
    cwd: Path,
    timeout: float = 120.0,
) -> tuple[int, str]:
    """Run a git command and return (returncode, stdout)."""
    env = {**os.environ}
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
    )
    try:
        stdout_bytes, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"git command timed out after {timeout}s: {' '.join(args)}")
    return proc.returncode, stdout_bytes.decode(errors="replace")


def head_sha(workspace: Path) -> str:
    """Return HEAD SHA synchronously at turn start (FR-007).

    This is sync because it is called before async operations start.
    Uses git rev-parse directly via subprocess.
    """
    import subprocess

    try:
        result = subprocess.run(
            [_GIT, "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Failed to get HEAD SHA: {exc}")


async def changed_paths_since(workspace: Path, since_sha: str) -> dict[str, str]:
    """Return changed paths since SHA as {path -> 'added'|'modified'|'deleted'}.

    Includes both committed changes (via git diff) and untracked/unstaged
    changes (via git status --porcelain).
    """
    paths: dict[str, str] = {}

    rc, stdout = await _run_git_stdout(
        [_GIT, "diff", "--name-status", since_sha],
        workspace,
    )
    if rc == 0:
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                status, path = parts
                if status == "A":
                    paths[path] = "added"
                elif status == "M":
                    paths[path] = "modified"
                elif status == "D":
                    paths[path] = "deleted"

    rc, stdout = await _run_git_stdout(
        [_GIT, "status", "--porcelain", "-uall"],
        workspace,
    )
    if rc == 0:
        # Do not strip the whole output: the first porcelain line's leading
        # space is part of its two-column status (" D .lint_fail").
        for line in stdout.splitlines():
            if not line.strip():
                continue
            status = line[:2]
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if status.startswith("?") or "A" in status:
                paths[path] = "added"
            elif "D" in status:
                paths[path] = "deleted"
            else:
                paths[path] = "modified"

    return dict(sorted(paths.items()))


async def squash_turn_commits(workspace: Path, start_sha: str) -> int:
    """Squash all commits from start_sha into the index (FR-007).

    Runs git reset --soft <start_sha> to move all commits back to the index.
    Only runs when HEAD != start_sha. Returns the number of commits squashed.
    """
    head = head_sha(workspace)
    if head == start_sha:
        return 0

    rc, stderr = await _run_git(
        [_GIT, "reset", "--soft", start_sha],
        workspace,
    )
    if rc != 0:
        raise RuntimeError(f"git reset --soft failed: {stderr}")

    rc, stdout = await _run_git_stdout(
        [_GIT, "rev-list", "--count", f"{start_sha}..{head}"],
        workspace,
    )
    if rc == 0:
        try:
            return int(stdout.strip())
        except ValueError:
            return 0
    return 0


async def revert_paths(workspace: Path, paths: list[str]) -> list[str]:
    """Revert specified paths: tracked via git checkout, untracked via rm.

    Returns list of paths that were actually reverted.
    """
    if not paths:
        return []

    reverted = []

    for path in paths:
        path_obj = workspace / path

        # Unstage first: ls-files reads the index, so a staged-but-never-
        # committed path would be reported as tracked and "restored" by
        # checkout from its own staged blob, leaving the content in the index
        # for the next index-wide commit (#396).
        await unstage_paths(workspace, [path])

        rc, stdout = await _run_git_stdout(
            [_GIT, "ls-files", "--", path],
            workspace,
        )
        is_tracked = rc == 0 and stdout.strip()

        if is_tracked:
            rc, _stderr = await _run_git(
                [_GIT, "checkout", "--", path],
                workspace,
            )
            if rc == 0:
                reverted.append(path)
        elif path_obj.is_symlink() or path_obj.exists():
            if path_obj.is_dir() and not path_obj.is_symlink():
                shutil.rmtree(path_obj, ignore_errors=True)
            else:
                path_obj.unlink(missing_ok=True)
            reverted.append(path)

    return reverted


async def commit_paths(workspace: Path, paths: list[str], message: str) -> str | None:
    """Commit only the specified paths and return the new SHA, or None.

    Stages exactly those paths using git add -A -- <paths> (which handles
    both additions and deletions), then commits with the message.
    Returns None if nothing was staged.
    """
    if not paths:
        return None

    rc, _stderr = await _run_git(
        [_GIT, "config", "user.name", "coordinare-performer"],
        workspace,
    )
    if rc != 0:
        raise RuntimeError("Failed to set git user.name")

    rc, _stderr = await _run_git(
        [_GIT, "config", "user.email", "coordinare@noreply"],
        workspace,
    )
    if rc != 0:
        raise RuntimeError("Failed to set git user.email")

    rc, _stderr = await _run_git(
        [_GIT, "add", "-A", "--"] + paths,
        workspace,
    )
    if rc != 0:
        raise RuntimeError(f"git add failed: {_stderr}")

    rc, _stderr = await _run_git(
        [_GIT, "diff", "--cached", "--quiet"],
        workspace,
    )
    if rc == 0:
        return None

    rc, _stderr = await _run_git(
        [_GIT, "commit", "-m", message],
        workspace,
    )
    if rc != 0:
        # A pre-commit hook likely refused the commit. Record its output, then
        # retry with --no-verify: the salvage commit must not wedge the run,
        # and the hook run is preserved in the log for the review record.
        log.warning("implementer.commit_hook_failed", stderr=_stderr[-2000:])
        rc, _stderr = await _run_git(
            [_GIT, "commit", "--no-verify", "-m", message],
            workspace,
        )
        if rc != 0:
            raise RuntimeError(f"git commit failed: {_stderr}")

    return head_sha(workspace)


async def branch_commit_entries(
    workspace: Path,
    base_candidates: list[str],
) -> list[tuple[str, list[str]]]:
    """The commits this branch carries over its base, newest first (spec 171 FR-001).

    Returns ``(subject, paths)`` per commit on ``<base>..HEAD`` for the first
    candidate ref that resolves, and ``[]`` when none does or the range is
    empty. The resume rule reads this to find the commits an EARLIER run of the
    same card left on the branch; a history it cannot read leaves every resume
    rule inert, which is the pre-171 behaviour.
    """
    base = None
    for candidate in base_candidates:
        if not candidate:
            continue
        rc, _out = await _run_git_stdout([_GIT, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], workspace)
        if rc == 0:
            base = candidate
            break
    if base is None:
        return []

    rc, stdout = await _run_git_stdout(
        # core.quotepath defaults to true, which renders a non-ASCII path as an
        # octal-escaped quoted string ("src/\346\226\207.py"). That path then
        # matches nothing on disk, so the resume rule silently never engages for
        # such a repository.
        [_GIT, "-c", "core.quotepath=false", "log", "--no-merges", "--format=%x00%s", "--name-only", f"{base}..HEAD"],
        workspace,
    )
    if rc != 0:
        return []

    entries: list[tuple[str, list[str]]] = []
    # one record per commit: NUL, the subject, a blank line, then its paths
    for chunk in stdout.split("\0"):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        subject = lines[0].strip()
        paths = [line.strip() for line in lines[1:] if line.strip()]
        entries.append((subject, paths))
    return entries

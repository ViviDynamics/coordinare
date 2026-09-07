"""Git helpers for commit squashing and scope reversion (spec 167).

Pure async functions that manipulate git state: getting SHAs, tracking changes,
squashing turns into step commits, reverting out-of-scope edits.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

__all__ = [
    "head_sha",
    "changed_paths_since",
    "squash_turn_commits",
    "revert_paths",
    "commit_paths",
]


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
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
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
        ["git", "diff", "--name-status", since_sha],
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
        ["git", "status", "--porcelain", "-uall"],
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
        ["git", "reset", "--soft", start_sha],
        workspace,
    )
    if rc != 0:
        raise RuntimeError(f"git reset --soft failed: {stderr}")

    rc, stdout = await _run_git_stdout(
        ["git", "rev-list", "--count", f"{start_sha}..{head}"],
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

        rc, stdout = await _run_git_stdout(
            ["git", "ls-files", "--", path],
            workspace,
        )
        is_tracked = rc == 0 and stdout.strip()

        if is_tracked:
            rc, _stderr = await _run_git(
                ["git", "checkout", "--", path],
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
        ["git", "config", "user.name", "coordinare-performer"],
        workspace,
    )
    if rc != 0:
        raise RuntimeError("Failed to set git user.name")

    rc, _stderr = await _run_git(
        ["git", "config", "user.email", "coordinare@noreply"],
        workspace,
    )
    if rc != 0:
        raise RuntimeError("Failed to set git user.email")

    rc, _stderr = await _run_git(
        ["git", "add", "-A", "--"] + paths,
        workspace,
    )
    if rc != 0:
        raise RuntimeError(f"git add failed: {_stderr}")

    rc, _stderr = await _run_git(
        ["git", "diff", "--cached", "--quiet"],
        workspace,
    )
    if rc == 0:
        return None

    rc, _stderr = await _run_git(
        ["git", "commit", "-m", message],
        workspace,
    )
    if rc != 0:
        raise RuntimeError(f"git commit failed: {_stderr}")

    return head_sha(workspace)

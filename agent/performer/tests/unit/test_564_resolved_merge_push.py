"""Publish resolved merges without flattening them or overwriting concurrent work."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from performer.workspace import WorkspaceSetupError, _push_head_without_clobbering, _run_git, _run_git_stdout


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=QA", "-c", "user.email=qa@example.invalid", *args],
        check=check, capture_output=True, text=True,
    )


def _resolved_merge(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "origin.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "-b", "main")
    _git(local, "remote", "add", "origin", str(remote))
    sample = local / "sample"
    sample.write_text("base\n")
    _git(local, "add", ".")
    _git(local, "commit", "-m", "base")
    _git(local, "checkout", "-b", "feature")
    sample.write_text("feature\n")
    _git(local, "commit", "-am", "feature")
    _git(local, "push", "origin", "feature")
    _git(local, "checkout", "main")
    sample.write_text("main\n")
    _git(local, "commit", "-am", "main")
    _git(local, "checkout", "feature")
    assert _git(local, "merge", "main", check=False).returncode != 0
    sample.write_text("feature\nmain\n")
    _git(local, "add", ".")
    _git(local, "commit", "-m", "resolve both contents")
    return local, remote


@pytest.mark.asyncio
@pytest.mark.parametrize("untracked_file", [False, True])
async def test_resolved_merge_is_pushed_intact(tmp_path: Path, untracked_file: bool) -> None:
    local, remote = _resolved_merge(tmp_path)
    if untracked_file:
        (local / "scratch.log").write_text("local scratch file\n")
    merged = _git(local, "rev-parse", "HEAD").stdout.strip()

    async def run(args: list[str], what: str) -> tuple[int, str]:
        return await _run_git(["git", "-C", str(local), *args], cwd=None, env=dict(os.environ))

    async def out(args: list[str], what: str) -> tuple[int, str]:
        return await _run_git_stdout(["git", "-C", str(local), *args], cwd=None, env=dict(os.environ))

    await _push_head_without_clobbering(run, out, remote="origin", branch="feature", score=None)
    assert _git(remote, "rev-parse", "feature").stdout.strip() == merged
    assert _git(remote, "show", "feature:sample").stdout == "feature\nmain\n"
    assert len(_git(remote, "rev-list", "--parents", "-n", "1", "feature").stdout.split()) == 3


@pytest.mark.asyncio
async def test_remote_update_after_fetch_is_not_overwritten(tmp_path: Path) -> None:
    local, remote = _resolved_merge(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-b", "feature", str(remote), str(other))
    (other / "concurrent").write_text("other writer\n")
    _git(other, "add", ".")
    _git(other, "commit", "-m", "concurrent update")
    competing_head = _git(other, "rev-parse", "HEAD").stdout.strip()

    async def run(args: list[str], what: str) -> tuple[int, str]:
        if args[0] == "push":
            _git(other, "push", "origin", "feature")
        assert "--force" not in args
        return await _run_git(["git", "-C", str(local), *args], cwd=None, env=dict(os.environ))

    async def out(args: list[str], what: str) -> tuple[int, str]:
        return await _run_git_stdout(["git", "-C", str(local), *args], cwd=None, env=dict(os.environ))

    with pytest.raises(WorkspaceSetupError, match="git push failed"):
        await _push_head_without_clobbering(run, out, remote="origin", branch="feature", score=None)
    assert _git(remote, "rev-parse", "feature").stdout.strip() == competing_head
    assert _git(remote, "show", "feature:concurrent").stdout == "other writer\n"

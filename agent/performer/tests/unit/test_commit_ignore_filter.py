"""Doc-commit must not let a gitignored model-chosen path poison the batch.

A tech_writer model occasionally emits a doc path under a gitignored dir (e.g.
Rails' .bundle); `git add` of an explicitly-named ignored path exits non-zero
and used to fail the whole documenting commit (blocked #171). `_git_ignored_subset`
identifies exactly those paths so commit_files can drop them.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from performer.workspace import _git_ignored_subset


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / ".gitignore").write_text(".bundle/\nvendor/bundle/\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_identifies_only_ignored_paths(repo: Path) -> None:
    (repo / ".bundle").mkdir()
    (repo / ".bundle" / "config").write_text("x", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("y", encoding="utf-8")
    ignored = await _git_ignored_subset(
        [".bundle/config", "docs/guide.md"], repo, {**os.environ},
    )
    assert ignored == {".bundle/config"}


@pytest.mark.asyncio
async def test_no_ignored_paths_returns_empty(repo: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("a", encoding="utf-8")
    ignored = await _git_ignored_subset(["docs/a.md", "README.md"], repo, {**os.environ})
    assert ignored == set()


@pytest.mark.asyncio
async def test_empty_input_returns_empty(repo: Path) -> None:
    assert await _git_ignored_subset([], repo, {**os.environ}) == set()


@pytest.mark.asyncio
async def test_filtered_list_is_git_add_safe(repo: Path) -> None:
    """The surviving (non-ignored) paths must `git add` cleanly — i.e. dropping
    the ignored subset is exactly what unblocks the batch commit."""
    (repo / ".bundle").mkdir()
    (repo / ".bundle" / "config").write_text("x", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("y", encoding="utf-8")
    paths = [".bundle/config", "docs/guide.md"]
    ignored = await _git_ignored_subset(paths, repo, {**os.environ})
    survivors = [p for p in paths if p not in ignored]
    # git add of the survivors must succeed (exit 0)
    r = subprocess.run(["git", "add", "--", *survivors], cwd=repo)
    assert r.returncode == 0
    assert survivors == ["docs/guide.md"]

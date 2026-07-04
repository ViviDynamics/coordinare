"""124: the documenter may RETIRE dead wiki pages via commit_files' `deletions`.

Deletion is a sharp tool, so `_safe_doc_deletions` restricts it to `docs/` (never
source or root files) and rejects absolute/`..` paths; `commit_files` git-rm's the
surviving paths in the SAME commit as any writes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from performer.workspace import Stand, _safe_doc_deletions, commit_files


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# _safe_doc_deletions — path-safety filter (pure)
# --------------------------------------------------------------------------
def test_safe_doc_deletions_allows_docs_paths() -> None:
    assert _safe_doc_deletions(["docs/wiki/dead.md", "docs/cards/1/x.md"]) == [
        "docs/wiki/dead.md", "docs/cards/1/x.md",
    ]


def test_safe_doc_deletions_rejects_non_docs_and_root() -> None:
    # Source files and root files (AGENTS.md/README.md) must never be deletable.
    assert _safe_doc_deletions(["src/app.py", "AGENTS.md", "README.md", "config/routes.rb"]) == []


def test_safe_doc_deletions_rejects_absolute_and_traversal() -> None:
    assert _safe_doc_deletions(["/etc/passwd", "docs/../src/secret.py", "../docs/x.md"]) == []


def test_safe_doc_deletions_ignores_junk() -> None:
    assert _safe_doc_deletions([None, 123, "", "docs/keep.md"]) == ["docs/keep.md"]  # type: ignore[list-item]
    assert _safe_doc_deletions(None) == []


# --------------------------------------------------------------------------
# commit_files — end-to-end deletion (local repo + bare remote so push works)
# --------------------------------------------------------------------------
@pytest.fixture
def stand(tmp_path: Path) -> Stand:
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], tmp_path)
    work = tmp_path / "work"
    _git(["clone", str(remote), str(work)], tmp_path)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    _git(["checkout", "-b", "feat/x"], work)
    (work / "docs" / "wiki").mkdir(parents=True)
    (work / "docs" / "wiki" / "README.md").write_text("# Wiki\n", encoding="utf-8")
    (work / "docs" / "wiki" / "stale.md").write_text("# Stale\nold content\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "seed"], work)
    _git(["push", "-u", "origin", "feat/x"], work)
    s = Stand(path=work, branch="feat/x")
    s.git_env = {}
    return s


@pytest.mark.asyncio
async def test_commit_files_retires_a_dead_page(stand: Stand) -> None:
    """A deletion git-rm's the page (removed from disk) and lands in the commit
    alongside a rewrite, and the path is returned in the changed list."""
    changed = await commit_files(
        stand,
        [{"path": "docs/wiki/README.md", "content": "# Wiki\nrefreshed\n"}],
        "docs: prune",
        deletions=["docs/wiki/stale.md"],
    )
    assert "docs/wiki/stale.md" in changed
    assert "docs/wiki/README.md" in changed
    assert not (stand.path / "docs" / "wiki" / "stale.md").exists()  # removed from disk
    name_status = subprocess.run(
        ["git", "log", "--name-status", "-1", "--format="], cwd=stand.path,
        capture_output=True, text=True,
    ).stdout
    assert "D\tdocs/wiki/stale.md" in name_status
    assert "M\tdocs/wiki/README.md" in name_status


@pytest.mark.asyncio
async def test_commit_files_deletion_only_still_commits(stand: Stand) -> None:
    """A deletion with no file writes still produces a commit."""
    changed = await commit_files(stand, [], "docs: retire stale page",
                                 deletions=["docs/wiki/stale.md"])
    assert changed == ["docs/wiki/stale.md"]
    assert not (stand.path / "docs" / "wiki" / "stale.md").exists()


@pytest.mark.asyncio
async def test_commit_files_ignores_unsafe_deletion(stand: Stand) -> None:
    """A non-docs deletion path is filtered out; with nothing else to do, no commit."""
    (stand.path / "keep_me.py").write_text("x=1\n", encoding="utf-8")
    _git(["add", "keep_me.py"], stand.path)
    _git(["commit", "-m", "add source"], stand.path)
    changed = await commit_files(stand, [], "docs: noop", deletions=["keep_me.py"])
    assert changed == []
    assert (stand.path / "keep_me.py").exists()  # source untouched


@pytest.mark.asyncio
async def test_commit_files_nonexistent_deletion_is_noop(stand: Stand) -> None:
    """Deleting a path that doesn't exist is a safe no-op, not a failure."""
    changed = await commit_files(stand, [], "docs: noop",
                                 deletions=["docs/wiki/never-existed.md"])
    assert changed == []

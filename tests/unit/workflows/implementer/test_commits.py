"""Tests for git commit helpers (spec 167 FR-007)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from performer.workflows.implementer.commits import (
    branch_commit_entries,
    changed_paths_since,
    commit_paths,
    head_sha,
    revert_paths,
    squash_turn_commits,
)

_G = ["git", "-c", "user.email=e@x", "-c", "user.name=t"]


def _sh(args, cwd):
    """Run command; raise on failure."""
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _repo_with_remote(tmp_path: Path, *, branch: str = "test") -> tuple[Path, Path]:
    """A local clone on *branch* with one commit, and a bare origin that has it."""
    bare = tmp_path / "origin.git"
    _sh(["git", "init", "-q", "--bare", "-b", "main", str(bare)], tmp_path)
    local = tmp_path / "local"
    _sh(["git", "init", "-q", "-b", "main", str(local)], tmp_path)
    (local / "README.md").write_text("base\n")
    _sh([*_G, "add", "."], local)
    _sh([*_G, "commit", "-q", "-m", "base"], local)
    _sh(["git", "remote", "add", "origin", str(bare)], local)
    _sh(["git", "push", "-q", "origin", "main"], local)
    _sh(["git", "checkout", "-q", "-b", branch], local)
    _sh(["git", "push", "-q", "origin", branch], local)
    return local, bare


def test_head_sha_returns_current_sha(tmp_path):
    """head_sha returns the current HEAD SHA."""
    local, _ = _repo_with_remote(tmp_path)
    sha = head_sha(local)
    assert len(sha) == 40
    assert sha.isalnum()

    # Verify it matches git rev-parse HEAD
    result = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    assert sha == result.stdout.strip()


@pytest.mark.asyncio
async def test_changed_paths_since_includes_committed_changes(tmp_path):
    """changed_paths_since includes changes from git diff."""
    local, _ = _repo_with_remote(tmp_path)
    start_sha = head_sha(local)

    (local / "app.py").write_text("print('hello')\n")
    _sh([*_G, "add", "."], local)
    _sh([*_G, "commit", "-q", "-m", "add app"], local)

    (local / "README.md").write_text("updated\n")
    _sh([*_G, "add", "."], local)
    _sh([*_G, "commit", "-q", "-m", "update readme"], local)

    changes = await changed_paths_since(local, start_sha)
    assert "app.py" in changes
    assert changes["app.py"] == "added"
    assert "README.md" in changes
    assert changes["README.md"] == "modified"


@pytest.mark.asyncio
async def test_changed_paths_since_includes_untracked(tmp_path):
    """changed_paths_since includes untracked files."""
    local, _ = _repo_with_remote(tmp_path)
    start_sha = head_sha(local)

    (local / "new_file.txt").write_text("content\n")
    changes = await changed_paths_since(local, start_sha)
    assert "new_file.txt" in changes
    assert changes["new_file.txt"] == "added"


@pytest.mark.asyncio
async def test_changed_paths_since_sorted_unique(tmp_path):
    """changed_paths_since returns sorted unique paths."""
    local, _ = _repo_with_remote(tmp_path)
    start_sha = head_sha(local)

    (local / "z.txt").write_text("z\n")
    (local / "a.txt").write_text("a\n")
    _sh([*_G, "add", "."], local)
    _sh([*_G, "commit", "-q", "-m", "add files"], local)

    changes = await changed_paths_since(local, start_sha)
    keys = list(changes.keys())
    assert keys == sorted(keys), "paths should be sorted"


@pytest.mark.asyncio
async def test_squash_turn_commits_soft_reset(tmp_path):
    """squash_turn_commits runs git reset --soft."""
    local, _ = _repo_with_remote(tmp_path)
    start_sha = head_sha(local)

    (local / "a.py").write_text("a\n")
    _sh([*_G, "add", "."], local)
    _sh([*_G, "commit", "-q", "-m", "commit 1"], local)

    (local / "b.py").write_text("b\n")
    _sh([*_G, "add", "."], local)
    _sh([*_G, "commit", "-q", "-m", "commit 2"], local)

    count = await squash_turn_commits(local, start_sha)
    assert count == 2

    result = subprocess.run(
        ["git", "-C", str(local), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    assert "a.py" in result.stdout
    assert "b.py" in result.stdout
    # New files appear as "A  " (added), not "M" (modified)
    assert "A  a.py" in result.stdout or "A  b.py" in result.stdout


@pytest.mark.asyncio
async def test_squash_turn_commits_when_head_equals_sha(tmp_path):
    """squash_turn_commits returns 0 when HEAD == start_sha."""
    local, _ = _repo_with_remote(tmp_path)
    sha = head_sha(local)
    count = await squash_turn_commits(local, sha)
    assert count == 0


@pytest.mark.asyncio
async def test_revert_paths_tracked_file(tmp_path):
    """revert_paths reverts tracked file via git checkout."""
    local, _ = _repo_with_remote(tmp_path)

    (local / "file.txt").write_text("original\n")
    _sh([*_G, "add", "."], local)
    _sh([*_G, "commit", "-q", "-m", "add file"], local)

    (local / "file.txt").write_text("modified\n")
    reverted = await revert_paths(local, ["file.txt"])
    assert "file.txt" in reverted
    assert (local / "file.txt").read_text() == "original\n"


@pytest.mark.asyncio
async def test_revert_paths_untracked_file(tmp_path):
    """revert_paths deletes untracked file."""
    local, _ = _repo_with_remote(tmp_path)
    (local / "new.txt").write_text("new\n")

    reverted = await revert_paths(local, ["new.txt"])
    assert "new.txt" in reverted
    assert not (local / "new.txt").exists()


@pytest.mark.asyncio
async def test_revert_paths_leaves_other_files_alone(tmp_path):
    """revert_paths reverts only specified paths."""
    local, _ = _repo_with_remote(tmp_path)

    (local / "file.txt").write_text("original\n")
    (local / "other.txt").write_text("original\n")
    _sh([*_G, "add", "."], local)
    _sh([*_G, "commit", "-q", "-m", "add files"], local)

    (local / "file.txt").write_text("modified\n")
    (local / "other.txt").write_text("modified\n")

    reverted = await revert_paths(local, ["file.txt"])
    assert "file.txt" in reverted
    assert "other.txt" not in reverted
    assert (local / "file.txt").read_text() == "original\n"
    assert (local / "other.txt").read_text() == "modified\n"


@pytest.mark.asyncio
async def test_commit_paths_stages_and_commits(tmp_path):
    """commit_paths stages paths and commits them."""
    local, _ = _repo_with_remote(tmp_path)
    start_sha = head_sha(local)

    (local / "new.py").write_text("print('new')\n")
    (local / "app.py").write_text("old\n")

    sha = await commit_paths(local, ["new.py", "app.py"], "test commit")
    assert sha is not None
    assert sha != start_sha
    assert len(sha) == 40

    # Verify commit exists in log
    result = subprocess.run(
        ["git", "-C", str(local), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    assert "test commit" in result.stdout


@pytest.mark.asyncio
async def test_commit_paths_returns_none_when_nothing_staged(tmp_path):
    """commit_paths returns None when no changes to stage."""
    local, _ = _repo_with_remote(tmp_path)
    sha = await commit_paths(local, [], "empty commit")
    assert sha is None


@pytest.mark.asyncio
async def test_commit_paths_leaves_unrelated_files_uncommitted(tmp_path):
    """commit_paths commits only specified paths."""
    local, _ = _repo_with_remote(tmp_path)

    (local / "aaa.py").write_text("aaa\n")
    (local / "zzz.py").write_text("zzz\n")

    await commit_paths(local, ["aaa.py"], "only aaa")

    result = subprocess.run(
        ["git", "-C", str(local), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    assert "zzz.py" in result.stdout
    # After commit, aaa.py should not be in status (it's committed)
    assert "aaa.py" not in result.stdout


@pytest.mark.asyncio
async def test_revert_paths_removes_an_untracked_directory(tmp_path):
    """Live round: codex left .codex/.tmp/plugins and revert tried to unlink a directory."""
    import subprocess

    from performer.workflows.implementer.commits import revert_paths

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / ".codex" / ".tmp" / "plugins").mkdir(parents=True)
    (repo / ".codex" / ".tmp" / "plugins" / "a.json").write_text("{}")
    reverted = await revert_paths(repo, [".codex/.tmp/plugins"])
    assert reverted == [".codex/.tmp/plugins"] and not (repo / ".codex" / ".tmp" / "plugins").exists()


# --- spec 171: reading the branch's own history --------------------------------


def _commit(local: Path, rel: str, text: str, subject: str) -> None:
    path = local / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _sh([*_G, "add", "--", rel], local)
    _sh([*_G, "commit", "-q", "-m", subject], local)


@pytest.mark.asyncio
async def test_branch_commit_entries_returns_subjects_with_their_paths(tmp_path):
    """Each commit over the base comes back with the paths it touched (171 FR-001)."""
    local, _ = _repo_with_remote(tmp_path, branch="feat/x")
    _commit(local, "tests/test_m0.py", "t\n", "test(#7): failing tests for milestone 0")
    _commit(local, "src/m0.py", "M0\n", "feat(#7): milestone 0")

    entries = await branch_commit_entries(local, ["origin/main", "main"])

    assert entries == [
        ("feat(#7): milestone 0", ["src/m0.py"]),
        ("test(#7): failing tests for milestone 0", ["tests/test_m0.py"]),
    ]


@pytest.mark.asyncio
async def test_branch_commit_entries_reports_every_path_of_one_commit(tmp_path):
    local, _ = _repo_with_remote(tmp_path, branch="feat/x")
    (local / "src").mkdir()
    (local / "src" / "a.py").write_text("a\n")
    (local / "src" / "b.py").write_text("b\n")
    _sh([*_G, "add", "."], local)
    _sh([*_G, "commit", "-q", "-m", "feat(#7): both"], local)

    entries = await branch_commit_entries(local, ["main"])

    assert entries == [("feat(#7): both", ["src/a.py", "src/b.py"])]


@pytest.mark.asyncio
async def test_branch_commit_entries_is_empty_on_a_fresh_branch(tmp_path):
    """A branch with no commits of its own carries no history to resume from."""
    local, _ = _repo_with_remote(tmp_path, branch="feat/x")
    assert await branch_commit_entries(local, ["origin/main", "main"]) == []


@pytest.mark.asyncio
async def test_branch_commit_entries_is_empty_when_no_base_resolves(tmp_path):
    """An unreadable base leaves the resume rules inert rather than raising."""
    local, _ = _repo_with_remote(tmp_path, branch="feat/x")
    _commit(local, "src/m0.py", "M0\n", "feat(#7): milestone 0")
    assert await branch_commit_entries(local, ["origin/nope", "nope", ""]) == []


@pytest.mark.asyncio
async def test_branch_commit_entries_uses_the_first_base_that_resolves(tmp_path):
    local, _ = _repo_with_remote(tmp_path, branch="feat/x")
    _commit(local, "src/m0.py", "M0\n", "feat(#7): milestone 0")
    entries = await branch_commit_entries(local, ["origin/does-not-exist", "main"])
    assert entries == [("feat(#7): milestone 0", ["src/m0.py"])]


@pytest.mark.asyncio
async def test_branch_commit_entries_reports_non_ascii_paths_unquoted(tmp_path):
    """171 review: core.quotepath defaults to true, which would render this path
    as an octal-escaped quoted string that matches nothing on disk, so the resume
    rule would silently never engage for such a repository."""
    local, _ = _repo_with_remote(tmp_path, branch="feat/x")
    _commit(local, "src/\u6587\u4ef6.py", "x\n", "feat(#7): milestone 0")

    entries = await branch_commit_entries(local, ["main"])

    assert entries == [("feat(#7): milestone 0", ["src/\u6587\u4ef6.py"])]
    assert (local / "src" / "\u6587\u4ef6.py").exists(), "the reported path is the real one"

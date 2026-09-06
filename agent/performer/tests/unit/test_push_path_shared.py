"""165 review round (PR #266): every performer write goes through ONE push path.

Two defects found by the adversarial review, each pinned here:

* ``commit_file`` / ``commit_files`` pushed with ``--force`` directly, so the
  documenter side run (which commits through ``commit_files``) bypassed both
  the rebase-no-force rule and the documentation tree guard.
* the tree guard read ``git diff --name-only`` through the runner that returns
  stderr, so it saw an empty listing in production and refused nothing.
  The real-git tests below would have caught it.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from performer import workspace
from performer.models import Score, Stand
from performer.workspace import (
    WorkspaceSetupError,
    _enforce_documenter_tree,
    _run_git_stdout,
    commit_file,
    commit_files,
)

_G = ["git", "-c", "user.email=e@x", "-c", "user.name=t"]


def _sh(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _repo_with_remote(tmp_path: Path, *, branch: str = "feat/x") -> tuple[Path, Path]:
    """A local clone on *branch* with one commit, and a bare origin that has it."""
    bare = tmp_path / "origin.git"
    _sh(["git", "init", "-q", "--bare", "-b", "main", str(bare)], tmp_path)
    local = tmp_path / "local"
    _sh(["git", "init", "-q", "-b", "main", str(local)], tmp_path)
    (local / "README.md").write_text("base\n")
    _sh(_G + ["add", "."], local)
    _sh(_G + ["commit", "-q", "-m", "base"], local)
    _sh(["git", "remote", "add", "origin", str(bare)], local)
    _sh(["git", "push", "-q", "origin", "main"], local)
    _sh(["git", "checkout", "-q", "-b", branch], local)
    _sh(["git", "push", "-q", "origin", branch], local)
    return local, bare


def _score(role="documenting", brief=None) -> Score:
    return Score(
        card_id="c1", title="t", description="d", acceptance_criteria=[],
        repo_url="https://github.com/org/repo", branch="feat/x", base_branch="main", github_token="ghp",
        role=role,
        documentation_brief=brief if brief is not None else {"docs": [{"topic": "t", "location": "docs/x.md", "say": "s"}]},
    )


def _other_performer_pushes(bare: Path, tmp_path: Path, rel: str) -> None:
    """Simulate the implementer landing a commit on the same branch meanwhile."""
    other = tmp_path / "other"
    _sh(["git", "clone", "-q", "-b", "feat/x", str(bare), str(other)], tmp_path)
    (other / rel).parent.mkdir(parents=True, exist_ok=True)
    (other / rel).write_text("code\n")
    _sh(_G + ["add", "."], other)
    _sh(_G + ["commit", "-q", "-m", "implementer"], other)
    _sh(["git", "push", "-q", "origin", "feat/x"], other)


def _log_paths(bare: Path, branch: str) -> list[str]:
    out = subprocess.run(["git", "log", "--format=", "--name-only", branch], cwd=bare, check=True,
                         capture_output=True, text=True).stdout
    return sorted({line for line in out.splitlines() if line})


# --- the plumbing defect ------------------------------------------------------

@pytest.mark.asyncio
async def test_tree_guard_reads_the_listing_from_stdout(tmp_path):
    local, _ = _repo_with_remote(tmp_path)
    (local / "app").mkdir()
    (local / "app" / "model.rb").write_text("x\n")
    (local / "docs").mkdir()
    (local / "docs" / "a.md").write_text("x\n")
    _sh(_G + ["add", "."], local)
    _sh(_G + ["commit", "-q", "-m", "mixed"], local)

    async def git_out(args, what):
        return await _run_git_stdout(["git", "-C", str(local), *args], cwd=None, env=dict(os.environ))

    with pytest.raises(WorkspaceSetupError, match=r"app/model\.rb"):
        await _enforce_documenter_tree(git_out, "origin/feat/x", "docs/", "feat/x")


@pytest.mark.asyncio
async def test_run_git_stdout_returns_stdout_not_stderr(tmp_path):
    local, _ = _repo_with_remote(tmp_path)
    rc, out = await _run_git_stdout(["git", "-C", str(local), "rev-parse", "--abbrev-ref", "HEAD"], cwd=None, env=dict(os.environ))
    assert (rc, out.strip()) == (0, "feat/x")


# --- commit_files / commit_file share the no-clobber push ----------------------

@pytest.mark.asyncio
async def test_commit_files_rebases_onto_the_other_performer_instead_of_forcing(tmp_path):
    local, bare = _repo_with_remote(tmp_path)
    _other_performer_pushes(bare, tmp_path, "app/models/x.rb")
    stand = Stand(path=local, branch="feat/x")
    changed = await commit_files(stand, [{"path": "docs/wiki/x.md", "content": "doc\n"}], "docs", score=_score())
    assert changed == ["docs/wiki/x.md"]
    assert _log_paths(bare, "feat/x") == ["README.md", "app/models/x.rb", "docs/wiki/x.md"]


@pytest.mark.asyncio
async def test_commit_files_as_a_documenter_side_run_is_held_to_the_tree(tmp_path):
    local, bare = _repo_with_remote(tmp_path)
    stand = Stand(path=local, branch="feat/x")
    with pytest.raises(WorkspaceSetupError, match=r"documenter\.tree_violation.*app/models/x\.rb"):
        await commit_files(stand, [{"path": "app/models/x.rb", "content": "code\n"}], "docs", score=_score())
    assert _log_paths(bare, "feat/x") == ["README.md"], "nothing outside docs/ reached the remote"


@pytest.mark.asyncio
async def test_commit_files_without_a_brief_is_the_spec_125_pass_and_unguarded(tmp_path):
    local, bare = _repo_with_remote(tmp_path)
    stand = Stand(path=local, branch="feat/x")
    await commit_files(stand, [{"path": "docs/wiki/x.md", "content": "doc\n"}], "docs", score=_score(brief={}))
    assert "docs/wiki/x.md" in _log_paths(bare, "feat/x")


@pytest.mark.asyncio
async def test_commit_file_never_forces_over_a_concurrent_commit(tmp_path):
    local, bare = _repo_with_remote(tmp_path)
    _other_performer_pushes(bare, tmp_path, "spec/x_spec.rb")
    stand = Stand(path=local, branch="feat/x")
    await commit_file(stand, "docs/cards/plan.md", "plan\n", "plan")
    assert _log_paths(bare, "feat/x") == ["README.md", "docs/cards/plan.md", "spec/x_spec.rb"]


@pytest.mark.asyncio
async def test_commit_file_first_push_of_a_new_branch_still_lands(tmp_path):
    local, bare = _repo_with_remote(tmp_path)
    _sh(["git", "checkout", "-q", "-b", "feat/new"], local)
    stand = Stand(path=local, branch="feat/new")
    await commit_file(stand, "docs/cards/plan.md", "plan\n", "plan")
    assert "docs/cards/plan.md" in _log_paths(bare, "feat/new")


def test_no_direct_force_push_remains_outside_the_shared_path():
    """The only ``--force`` in workspace.py is the first-push fallback inside
    the shared helper; a second one is a regression of this review round."""
    src = Path(workspace.__file__).read_text()
    assert src.count('"--force"') == 1, "a second --force push crept back into workspace.py"


@pytest.mark.asyncio
async def test_documenting_role_passes_its_score_to_commit_files():
    """main.py's documenting commit must hand the Score over, or the tree
    guard never sees the documentation brief."""
    import inspect

    from performer import main as perf_main

    src = inspect.getsource(perf_main)
    call = src[src.index("committed = await commit_files("):]
    call = call[: call.index(")")]
    assert "score=perf.score" in call
    await asyncio.sleep(0)

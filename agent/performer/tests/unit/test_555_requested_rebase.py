"""555: the implementer rebases a diverged branch and publishes it under a lease.

Coordinare never rebases. It sends one relay_feedback entry
(``kind: branch_divergence``) carrying the head it classified and the base
revision it saw. The performer rebases the workspace before the agent starts
and, at push time, overwrites the remote branch only with a force-with-lease
pinned to that exact head.

These tests run real git against a local bare remote, because the property
under test is what git does to the remote ref.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from performer import workspace
from performer.models import Score
from performer.workspace import WorkspaceSetupError

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}
_BRANCH = "coordinare/card/work"
_GIT = shutil.which("git") or "git"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [_GIT, *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Repo:
    """A bare remote with a diverged branch, and the performer's clone of it."""

    def __init__(self, tmp: Path, *, conflict: bool = False) -> None:
        self.remote = tmp / "remote.git"
        seed = tmp / "seed"
        _git(tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        _git(tmp, "clone", "-q", str(self.remote), str(seed))
        _commit(seed, "shared.txt", "base\n", "base")
        _git(seed, "push", "-q", "origin", "HEAD:main")
        _git(seed, "checkout", "-q", "-b", _BRANCH)
        self.branch_head = _commit(
            seed, "shared.txt" if conflict else "feature.txt", "feature\n", "feature",
        )
        _git(seed, "push", "-q", "origin", _BRANCH)
        _git(seed, "checkout", "-q", "main")
        self.main_sha = _commit(
            seed, "shared.txt" if conflict else "main.txt", "main moved\n", "main moved",
        )
        _git(seed, "push", "-q", "origin", "HEAD:main")
        self.seed = seed
        # The performer's workspace: default branch cloned, then the branch checked out.
        self.stand = tmp / "stand"
        _git(tmp, "clone", "-q", str(self.remote), str(self.stand))
        _git(self.stand, "fetch", "-q", "origin", f"{_BRANCH}:{_BRANCH}")
        _git(self.stand, "checkout", "-q", _BRANCH)

    def remote_head(self) -> str:
        return _git(self.remote, "rev-parse", f"refs/heads/{_BRANCH}")

    def request(self, **overrides) -> dict:
        entry = {"kind": "branch_divergence", "base_branch": "main", "main_sha": self.main_sha,
                 "expected_head_sha": self.branch_head, "body": "rebase please"}
        entry.update(overrides)
        return entry

    def score(self, *, role: str = "implementing", feedback: list | None = None) -> Score:
        return Score(
            card_id="card", title="t", description="d", acceptance_criteria=[],
            repo_url="https://github.com/org/repo", branch=_BRANCH, github_token="ghp_secret",
            role=role, relay_feedback=[self.request()] if feedback is None else feedback,
        )

    async def setup_rebase(self, score: Score) -> None:
        await workspace._rebase_onto_base(self.stand, _ENV, score, workspace._rebase_request(score))

    async def push(self, score: Score) -> None:
        base = ["git", "-C", str(self.stand)]

        async def run(args, what):
            return await workspace._run_git(base + args, cwd=None, env=_ENV)

        async def out(args, what):
            return await workspace._run_git_stdout(base + args, cwd=None, env=_ENV)

        await workspace._push_head_without_clobbering(
            run, out, remote=str(self.remote), branch=_BRANCH, score=score,
        )


# --- the request ------------------------------------------------------------- #


def test_only_the_implementer_honours_a_complete_request(tmp_path) -> None:
    repo = _Repo(tmp_path)
    assert workspace._rebase_request(repo.score()) == repo.request()
    assert workspace._rebase_request(repo.score(role="reviewing")) is None
    assert workspace._rebase_request(repo.score(feedback=[{"body": "plain review note"}])) is None
    assert workspace._rebase_request(repo.score(feedback=[repo.request(expected_head_sha="")])) is None
    assert workspace._rebase_request(repo.score(feedback=[repo.request(main_sha="")])) is None
    assert workspace._rebase_request(None) is None


# --- clean rebase: no model needed ------------------------------------------- #


@pytest.mark.asyncio
async def test_behind_branch_is_rebased_at_setup_and_published_under_the_lease(tmp_path) -> None:
    repo = _Repo(tmp_path)
    score = repo.score()
    await repo.setup_rebase(score)

    head = _git(repo.stand, "rev-parse", "HEAD")
    assert head != repo.branch_head
    assert _git(repo.stand, "merge-base", "HEAD", "origin/main") == repo.main_sha
    assert repo.remote_head() == repo.branch_head  # nothing published yet

    await repo.push(score)
    assert repo.remote_head() == head
    assert (repo.stand / "feature.txt").exists() and (repo.stand / "main.txt").exists()


@pytest.mark.asyncio
async def test_setup_rebase_is_skipped_when_the_head_already_moved(tmp_path) -> None:
    """Someone pushed after coordinare classified the branch: the lease would be
    stale, so history is left alone."""
    repo = _Repo(tmp_path)
    score = repo.score(feedback=[repo.request(expected_head_sha="0" * 40)])
    await repo.setup_rebase(score)
    assert _git(repo.stand, "rev-parse", "HEAD") == repo.branch_head


# --- conflict: left for the agent -------------------------------------------- #


@pytest.mark.asyncio
async def test_conflicting_rebase_is_aborted_and_left_for_the_agent(tmp_path) -> None:
    repo = _Repo(tmp_path, conflict=True)
    score = repo.score()
    await repo.setup_rebase(score)
    assert _git(repo.stand, "rev-parse", "HEAD") == repo.branch_head
    assert _git(repo.stand, "status", "--porcelain") == ""
    assert not (repo.stand / ".git" / "rebase-merge").exists()

    # The agent then resolves it by hand and the performer publishes the result.
    subprocess.run([_GIT, "rebase", "origin/main"], cwd=repo.stand, env=_ENV, capture_output=True, check=False)
    (repo.stand / "shared.txt").write_text("main moved\nfeature\n")
    _git(repo.stand, "add", "shared.txt")
    _git(repo.stand, "-c", "core.editor=true", "rebase", "--continue")
    head = _git(repo.stand, "rev-parse", "HEAD")

    await repo.push(score)
    assert repo.remote_head() == head


@pytest.mark.asyncio
async def test_an_unfinished_rebase_is_never_pushed(tmp_path) -> None:
    repo = _Repo(tmp_path, conflict=True)
    subprocess.run([_GIT, "rebase", "origin/main"], cwd=repo.stand, env=_ENV, capture_output=True, check=False)
    with pytest.raises(WorkspaceSetupError, match="rebase_in_progress"):
        await repo.push(repo.score())
    assert repo.remote_head() == repo.branch_head


# --- the lease: nobody else's commits are overwritten ------------------------ #


@pytest.mark.asyncio
async def test_a_concurrent_push_makes_the_lease_stale_and_nothing_is_overwritten(tmp_path) -> None:
    """165 FR-013 survives: the documenter (or a human) pushed to the branch
    after coordinare classified it. The rebased branch must not replace it."""
    repo = _Repo(tmp_path)
    score = repo.score()
    await repo.setup_rebase(score)

    _git(repo.seed, "checkout", "-q", _BRANCH)
    other = _commit(repo.seed, "docs.txt", "docs\n", "documenter side run")
    _git(repo.seed, "push", "-q", "origin", _BRANCH)

    with pytest.raises(WorkspaceSetupError, match="rebase_lease_stale"):
        await repo.push(score)
    assert repo.remote_head() == other


@pytest.mark.asyncio
async def test_rewritten_history_that_is_not_the_requested_rebase_is_not_forced(tmp_path) -> None:
    """History rewritten some other way (the base coordinare saw is not in it)
    falls back to the ordinary no-force path: whatever that publishes sits on
    top of the remote's commit, which is never replaced."""
    repo = _Repo(tmp_path)
    _git(repo.stand, "commit", "-q", "--amend", "-m", "amended, not rebased")
    try:
        await repo.push(repo.score())
    except WorkspaceSetupError:
        pass
    _git(repo.remote, "merge-base", "--is-ancestor", repo.branch_head, f"refs/heads/{_BRANCH}")


@pytest.mark.asyncio
async def test_without_a_request_rewritten_history_is_never_forced(tmp_path) -> None:
    repo = _Repo(tmp_path)
    await repo.setup_rebase(repo.score())
    rebased = _git(repo.stand, "rev-parse", "HEAD")
    for score in (repo.score(feedback=[]), repo.score(role="documenting")):
        try:
            await repo.push(score)
        except WorkspaceSetupError:
            pass
        assert repo.remote_head() != rebased
        # Whatever the no-force path did, the original commit is still in the remote history.
        _git(repo.remote, "merge-base", "--is-ancestor", repo.branch_head, f"refs/heads/{_BRANCH}")


@pytest.mark.asyncio
async def test_a_merge_of_the_base_still_publishes_as_a_plain_fast_forward(tmp_path) -> None:
    """An agent that merged instead of rebasing is not punished: the remote
    head is an ancestor, so the ordinary push applies and no lease is used."""
    repo = _Repo(tmp_path)
    _git(repo.stand, "merge", "-q", "--no-edit", "origin/main")
    head = _git(repo.stand, "rev-parse", "HEAD")
    await repo.push(repo.score())
    assert repo.remote_head() == head
    _git(repo.remote, "merge-base", "--is-ancestor", repo.branch_head, head)


@pytest.mark.asyncio
async def test_uncommitted_tracked_changes_block_the_lease_push(tmp_path) -> None:
    repo = _Repo(tmp_path)
    score = repo.score()
    await repo.setup_rebase(score)
    (repo.stand / "feature.txt").write_text("half-finished\n")
    with pytest.raises(WorkspaceSetupError, match="uncommitted_changes"):
        await repo.push(score)
    assert repo.remote_head() == repo.branch_head

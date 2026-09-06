"""165 (FR-015 prerequisite): push_branch rebases onto the remote and never
forces over someone else's commits.

Two performers now write to one branch at the same time (implementer and the
documenter side run). The pre-165 fallback of ``git push --force`` on any
failure would let the loser overwrite the winner's commits. These tests pin the
new sequence by recording every git invocation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from performer import workspace
from performer.models import Score, Stand
from performer.workspace import WorkspaceSetupError, push_branch


def _score() -> Score:
    return Score(
        card_id="c1",
        title="t",
        description="d",
        acceptance_criteria=[],
        repo_url="https://github.com/org/repo",
        branch="feat/x",
        github_token="ghp_secret",
    )


class _Git:
    """Fake _run_git: answers by the git subcommand, records the sequence."""

    def __init__(self, answers: dict[str, tuple[int, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    async def __call__(self, args, cwd, env, timeout=120.0):
        self.calls.append(list(args))
        if "--force" in args:
            return self.answers.get("--force", (0, ""))
        sub = next((a for a in args if a in self.answers), None)
        return self.answers.get(sub, (0, ""))

    def subcommands(self) -> list[str]:
        out = []
        for c in self.calls:
            if "--force" in c:
                out.append("push --force")
            else:
                out.append(next(a for a in c[3:] if not a.startswith("-")))
        return out


@pytest.fixture
def stand(tmp_path: Path) -> Stand:
    return Stand(path=tmp_path, branch="feat/x")


@pytest.fixture(autouse=True)
def _no_artifact_guard(monkeypatch):
    async def _noop(stand, env):
        return None

    monkeypatch.setattr(workspace, "strip_agent_artifacts", _noop)


@pytest.mark.asyncio
async def test_existing_remote_branch_is_fetched_and_rebased_then_pushed_without_force(stand, monkeypatch):
    git = _Git({"ls-remote": (0, "abc\trefs/heads/feat/x")})
    monkeypatch.setattr(workspace, "_run_git", git)

    await push_branch(stand, _score())

    assert git.subcommands() == ["ls-remote", "fetch", "rebase", "push"]
    assert not any("--force" in c for c in git.calls)
    rebase = next(c for c in git.calls if "rebase" in c)
    assert "FETCH_HEAD" in rebase


@pytest.mark.asyncio
async def test_a_rebase_conflict_aborts_and_pushes_nothing(stand, monkeypatch):
    git = _Git({"ls-remote": (0, "abc\trefs/heads/feat/x"), "rebase": (1, "CONFLICT (content): db/schema.rb")})
    monkeypatch.setattr(workspace, "_run_git", git)

    with pytest.raises(WorkspaceSetupError, match="rebase_conflict"):
        await push_branch(stand, _score())

    subs = git.subcommands()
    assert "push" not in subs and "push --force" not in subs
    assert any("--abort" in c for c in git.calls), "the failed rebase must be aborted"


@pytest.mark.asyncio
async def test_a_non_fast_forward_rejection_is_never_forced(stand, monkeypatch):
    """Even if the push is rejected after a rebase (a race with a third push),
    the answer is an error, not --force."""
    git = _Git({"ls-remote": (0, "abc\trefs/heads/feat/x"), "push": (1, "! [rejected] feat/x -> feat/x (non-fast-forward)")})
    monkeypatch.setattr(workspace, "_run_git", git)

    with pytest.raises(WorkspaceSetupError, match="git push failed"):
        await push_branch(stand, _score())

    assert "push --force" not in git.subcommands()


@pytest.mark.asyncio
async def test_a_missing_remote_branch_pushes_without_fetching_and_may_force_once(stand, monkeypatch):
    git = _Git({"ls-remote": (2, ""), "push": (1, "error: failed to push some refs")})
    monkeypatch.setattr(workspace, "_run_git", git)

    await push_branch(stand, _score())  # the force fallback succeeds (default answer 0)

    assert git.subcommands() == ["ls-remote", "push", "push --force"]


@pytest.mark.asyncio
async def test_an_unanswerable_ls_remote_is_treated_as_existing(stand, monkeypatch):
    """rc 1 (auth or network) must not be read as 'branch missing': that path
    is the only one allowed to force."""
    git = _Git({"ls-remote": (1, "fatal: could not read from remote")})
    monkeypatch.setattr(workspace, "_run_git", git)

    await push_branch(stand, _score())

    assert "push --force" not in git.subcommands()
    assert "fetch" in git.subcommands()


@pytest.mark.asyncio
async def test_fetch_failure_is_reported_and_nothing_is_pushed(stand, monkeypatch):
    git = _Git({"ls-remote": (0, "x"), "fetch": (128, "fatal: couldn't find remote ref")})
    monkeypatch.setattr(workspace, "_run_git", git)

    with pytest.raises(WorkspaceSetupError, match="fetch before push"):
        await push_branch(stand, _score())

    assert "push" not in git.subcommands()

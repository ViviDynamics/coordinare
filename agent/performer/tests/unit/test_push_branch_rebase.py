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
        self.answers = {"merge-base": (1, ""), **answers}
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


class _GitOut:
    """Fake _run_git_stdout: answers by subcommand, returns (rc, stdout)."""

    def __init__(self, answers: dict[str, tuple[int, str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    async def __call__(self, args, cwd=None, env=None, timeout=120.0):
        self.calls.append(list(args))
        sub = next((a for a in args if a in self.answers), None)
        return self.answers.get(sub, (0, ""))


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

    assert git.subcommands() == ["ls-remote", "fetch", "merge-base", "rebase", "push"]
    assert not any("--force" in c for c in git.calls)
    rebase = next(c for c in git.calls if "rebase" in c)
    assert "FETCH_HEAD" in rebase


@pytest.mark.asyncio
async def test_a_rebase_conflict_aborts_and_pushes_nothing(stand, monkeypatch):
    git = _Git({"ls-remote": (0, "abc\trefs/heads/feat/x"), "rebase": (1, "CONFLICT (content): db/schema.rb")})
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", _GitOut())  # clean tree

    with pytest.raises(WorkspaceSetupError, match="rebase_conflict"):
        await push_branch(stand, _score())

    subs = git.subcommands()
    assert "push" not in subs and "push --force" not in subs
    assert any("--abort" in c for c in git.calls), "the failed rebase must be aborted"


# ---------------------------------------------------------------------------
# 306: a dirty tree is not a conflict with the remote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dirty_tree_is_reported_as_uncommitted_changes_not_a_conflict(stand, monkeypatch):
    """The failure behind #306.

    git refuses to rebase while the tree has unstaged changes. That is the
    performer's own unfinished work, not a conflict with the remote's commits,
    and calling it `rebase_conflict` sent operators looking for a merge problem
    that did not exist. The paths are named so the real question (why did the
    role leave files uncommitted) is answerable from the error alone.
    """
    git = _Git({
        "ls-remote": (0, "abc\trefs/heads/feat/x"),
        "rebase": (1, "error: cannot rebase: You have unstaged changes."),
    })
    out = _GitOut({"status": (0, " M src/app.py\n?? notes.txt\n")})
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", out)

    with pytest.raises(WorkspaceSetupError) as excinfo:
        await push_branch(stand, _score())

    message = str(excinfo.value)
    assert "uncommitted_changes" in message
    assert "rebase_conflict" not in message
    assert "src/app.py" in message and "notes.txt" in message

    subs = git.subcommands()
    assert "push" not in subs and "push --force" not in subs
    assert any("--abort" in c for c in git.calls), "the failed rebase must still be aborted"


@pytest.mark.asyncio
async def test_a_genuine_conflict_on_a_clean_tree_still_says_rebase_conflict(stand, monkeypatch):
    """The other side: with nothing uncommitted, the conflict message is correct
    and must not be relabelled."""
    git = _Git({
        "ls-remote": (0, "abc\trefs/heads/feat/x"),
        "rebase": (1, "CONFLICT (content): db/schema.rb"),
    })
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", _GitOut({"status": (0, "")}))

    with pytest.raises(WorkspaceSetupError, match="rebase_conflict"):
        await push_branch(stand, _score())


@pytest.mark.asyncio
async def test_an_unreadable_status_falls_back_to_the_conflict_message(stand, monkeypatch):
    """Best effort: if `git status` cannot be read we do not invent a diagnosis."""
    git = _Git({
        "ls-remote": (0, "abc\trefs/heads/feat/x"),
        "rebase": (1, "error: cannot rebase: You have unstaged changes."),
    })
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", _GitOut({"status": (128, "not a git repository")}))

    with pytest.raises(WorkspaceSetupError, match="rebase_conflict"):
        await push_branch(stand, _score())


@pytest.mark.asyncio
async def test_a_raising_status_helper_falls_back_to_the_conflict_message(stand, monkeypatch):
    """Best effort must also cover the failure mode the rc!=0 test cannot reach:
    the status helper itself raising (OSError or the 120 s timeout surface as
    WorkspaceSetupError from the git closure). The conflict diagnosis has to
    survive that instead of being replaced by the helper's own error."""
    git = _Git({
        "ls-remote": (0, "abc\trefs/heads/feat/x"),
        "rebase": (1, "error: cannot rebase: You have unstaged changes."),
    })

    class _RaisingOut(_GitOut):
        async def __call__(self, args, cwd=None, env=None, timeout=120.0):
            if "status" in args:
                raise WorkspaceSetupError("git status failed: boom")
            return await super().__call__(args, cwd, env, timeout)

    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", _RaisingOut())

    with pytest.raises(WorkspaceSetupError, match="rebase_conflict"):
        await push_branch(stand, _score())


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


@pytest.mark.asyncio
async def test_the_shared_push_path_reports_a_dirty_tree_for_every_caller():
    """Review finding: `push_branch` is only one of three callers of the shared
    push path. The implementer lane (`workflows/implementer/__init__.py:110`)
    and `commit_file` drive `_push_head_without_clobbering` directly with their
    own runners from `_stand_git_runners`, and the implementer lane is where an
    agent most plausibly leaves work uncommitted. Drive it directly so the
    diagnostic is pinned for those callers too, not just via push_branch.
    """
    git = _Git({
        "ls-remote": (0, "abc\trefs/heads/feat/x"),
        "rebase": (1, "error: cannot rebase: You have unstaged changes."),
    })
    out = _GitOut({"status": (0, " M lib/thing.rb\n")})

    with pytest.raises(WorkspaceSetupError) as excinfo:
        await workspace._push_head_without_clobbering(
            lambda args, what: git(["git", "-C", "/w", *args], cwd=None, env={}),
            out,
            remote="https://example/repo.git", branch="feat/x", score=None,
        )

    assert "uncommitted_changes" in str(excinfo.value)
    assert "lib/thing.rb" in str(excinfo.value)
    assert out.calls, "the shared path must consult git status, not just push_branch"

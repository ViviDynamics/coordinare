"""165 FR-015: a documenter side run may only commit under the documentation
tree; anything else is refused before push. Other roles are unaffected."""
from __future__ import annotations


import pytest

from performer import workspace
from performer.models import Score, Stand
from performer.workspace import WorkspaceSetupError, paths_outside_tree, push_branch


def _score(role="documenting", brief=None, env=None) -> Score:
    return Score(
        card_id="c1", title="t", description="d", acceptance_criteria=[],
        repo_url="https://github.com/org/repo", branch="feat/x", base_branch="main", github_token="ghp",
        role=role,
        documentation_brief=brief if brief is not None else {"docs": [{"topic": "t", "location": "docs/wiki/x.md", "say": "s"}]},
        workflow_env=env or {},
    )


class _Git:
    def __init__(self, answers):
        self.answers, self.calls = {"merge-base": (1, ""), **answers}, []

    async def __call__(self, args, cwd, env, timeout=120.0):
        self.calls.append(list(args))
        if "--force" in args:
            return self.answers.get("--force", (0, ""))
        sub = next((a for a in args if a in self.answers), None)
        return self.answers.get(sub, (0, ""))

    def subs(self):
        return [("push --force" if "--force" in c else next(a for a in c[3:] if not a.startswith("-"))) for c in self.calls]


@pytest.fixture(autouse=True)
def _no_guard(monkeypatch):
    async def _noop(stand, env):
        return None
    monkeypatch.setattr(workspace, "strip_agent_artifacts", _noop)


def test_paths_outside_tree_is_pure():
    assert paths_outside_tree(["docs/wiki/a.md", "app/models/b.rb", "", "docs/x.md"], "docs/") == ["app/models/b.rb"]
    assert paths_outside_tree(["docs/wiki/a.md"], "docs/") == []


@pytest.mark.asyncio
async def test_documenter_commits_inside_the_tree_are_pushed(tmp_path, monkeypatch):
    git = _Git({"ls-remote": (0, "x"), "diff": (0, "docs/wiki/time-tracking.md\ndocs/wiki/README.md\n")})
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", git)
    await push_branch(Stand(path=tmp_path, branch="feat/x"), _score())
    assert git.subs() == ["ls-remote", "ls-files", "fetch", "diff", "merge-base", "rebase", "push"]


@pytest.mark.asyncio
async def test_a_commit_outside_the_tree_is_refused_before_any_push(tmp_path, monkeypatch):
    git = _Git({"ls-remote": (0, "x"), "diff": (0, "docs/wiki/a.md\napp/models/time_entry.rb\n")})
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", git)
    with pytest.raises(WorkspaceSetupError, match=r"tree_violation.*app/models/time_entry\.rb"):
        await push_branch(Stand(path=tmp_path, branch="feat/x"), _score())
    assert "push" not in git.subs() and "rebase" not in git.subs()


@pytest.mark.asyncio
async def test_first_push_compares_against_the_base_branch(tmp_path, monkeypatch):
    git = _Git({"ls-remote": (2, ""), "diff": (0, "app/x.rb\n")})
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", git)
    with pytest.raises(WorkspaceSetupError, match="tree_violation"):
        await push_branch(Stand(path=tmp_path, branch="feat/x"), _score())
    fetch = next(c for c in git.calls if "fetch" in c)
    assert fetch[-1] == "main", "the comparison base is the card's base branch"


@pytest.mark.asyncio
async def test_the_tree_root_is_configurable(tmp_path, monkeypatch):
    git = _Git({"ls-remote": (0, "x"), "diff": (0, "documentation/guide.md\n")})
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", git)
    await push_branch(Stand(path=tmp_path, branch="feat/x"), _score(env={"DOCUMENTER_TREE": "documentation"}))
    assert "push" in git.subs()


@pytest.mark.asyncio
async def test_the_guard_follows_the_repositorys_docs_root(tmp_path, monkeypatch):
    """415: a side run in an MkDocs or Sphinx repository commits under the docs
    root the discovery resolves -- doc/source here -- not under a hardcoded docs/."""
    git = _Git({"ls-remote": (0, "x"), "ls-files": (0, "doc/source/conf.py\ndoc/source/usage.md\n"),
                "diff": (0, "doc/source/usage.md\n")})
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", git)
    await push_branch(Stand(path=tmp_path, branch="feat/x"), _score())
    assert "push" in git.subs(), "the discovered docs root is the guard's tree"


@pytest.mark.asyncio
async def test_a_commit_outside_the_discovered_root_is_refused(tmp_path, monkeypatch):
    git = _Git({"ls-remote": (0, "x"), "ls-files": (0, "doc/source/conf.py\n"),
                "diff": (0, "docs/wiki/other.md\n")})
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", git)
    with pytest.raises(WorkspaceSetupError, match=r"tree_violation.*docs/wiki/other\.md"):
        await push_branch(Stand(path=tmp_path, branch="feat/x"), _score())
    assert "push" not in git.subs()


@pytest.mark.asyncio
async def test_other_roles_and_the_end_of_lifecycle_documenter_are_not_guarded(tmp_path, monkeypatch):
    """The guard keys on the side run (a documentation brief present). The
    implementer, and the spec-125 documenting pass with no brief, push as before."""
    for score in (_score(role="implementing"), _score(role="documenting", brief={})):
        git = _Git({"ls-remote": (0, "x"), "diff": (0, "app/anything.rb\n")})
        monkeypatch.setattr(workspace, "_run_git", git)
        monkeypatch.setattr(workspace, "_run_git_stdout", git)
        await push_branch(Stand(path=tmp_path, branch="feat/x"), score)
        assert "diff" not in git.subs() and "push" in git.subs()


@pytest.mark.asyncio
async def test_the_guard_honours_the_symphonys_docts_root_override(tmp_path, monkeypatch):
    """415 review: the guard uses the same root the workflow used -- a symphony
    that pinned DOCS_ROOT=handbook pushes handbook/ writes without a discovery
    fallback inventing docs/wiki under it."""
    git = _Git({"ls-remote": (0, "x"), "ls-files": (0, "README.md\n"), "diff": (0, "handbook/guide.md\n")})
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace, "_run_git_stdout", git)
    await push_branch(Stand(path=tmp_path, branch="feat/x"), _score(env={"DOCS_ROOT": "handbook"}))
    assert "push" in git.subs()

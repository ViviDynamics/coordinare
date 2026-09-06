"""165 US3 independent test: implementer and documenter push to one branch in
either order; with the rebase-before-push path both land and neither forces."""
from __future__ import annotations


import pytest

from performer import workspace
from performer.models import Score, Stand
from performer.workspace import push_branch


class _Remote:
    """A fake remote branch: a list of commits. fetch/rebase/push are modelled
    just enough to show ordering and force behaviour."""

    def __init__(self):
        self.commits: list[str] = ["base"]
        self.pushes: list[tuple[str, bool]] = []  # (who, forced)

    def git_for(self, who: str, local_commits: list[str], changed_paths: list[str]):
        remote = self

        async def run(args, cwd, env, timeout=120.0):
            sub = next((a for a in args[3:] if not a.startswith("-")), "")
            if sub == "ls-remote":
                return (0, "x") if len(remote.commits) > 1 or remote.commits != ["base"] else (2, "")
            if sub == "fetch":
                return 0, ""
            if sub == "diff":
                return 0, "\n".join(changed_paths)
            if sub == "rebase":
                # our commits now sit on top of whatever the remote has
                local_commits[:] = remote.commits + [c for c in local_commits if c not in remote.commits]
                return 0, ""
            if sub == "push":
                forced = "--force" in args
                remote.pushes.append((who, forced))
                if not forced and local_commits[: len(remote.commits)] != remote.commits:
                    return 1, "! [rejected] (non-fast-forward)"
                remote.commits = list(local_commits)
                return 0, ""
            return 0, ""

        return run


def _score(role: str, brief: dict | None = None) -> Score:
    return Score(card_id="c1", title="t", description="d", acceptance_criteria=[], repo_url="https://github.com/o/r",
                 branch="feat/x", base_branch="main", github_token="g", role=role, documentation_brief=brief or {})


@pytest.fixture(autouse=True)
def _no_guard(monkeypatch):
    async def _noop(stand, env):
        return None
    monkeypatch.setattr(workspace, "strip_agent_artifacts", _noop)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["implementer", "documenter"])
async def test_both_performers_land_on_one_branch_without_force(tmp_path, monkeypatch, first):
    remote = _Remote()
    remote.commits = ["base", "architect-none"]  # branch exists on the remote
    impl_local = ["base", "architect-none", "impl-1"]
    docs_local = ["base", "architect-none", "docs-1"]
    brief = {"docs": [{"topic": "t", "location": "docs/wiki/x.md", "say": "s"}]}

    async def push_impl():
        monkeypatch.setattr(workspace, "_run_git", remote.git_for("implementer", impl_local, ["app/models/x.rb", "spec/models/x_spec.rb"]))
        monkeypatch.setattr(workspace, "_run_git_stdout", remote.git_for("implementer", impl_local, ["app/models/x.rb", "spec/models/x_spec.rb"]))
        await push_branch(Stand(path=tmp_path, branch="feat/x"), _score("implementing"))

    async def push_docs():
        monkeypatch.setattr(workspace, "_run_git", remote.git_for("documenter", docs_local, ["docs/wiki/x.md"]))
        monkeypatch.setattr(workspace, "_run_git_stdout", remote.git_for("documenter", docs_local, ["docs/wiki/x.md"]))
        await push_branch(Stand(path=tmp_path, branch="feat/x"), _score("documenting", brief))

    if first == "implementer":
        await push_impl()
        await push_docs()
    else:
        await push_docs()
        await push_impl()

    assert set(remote.commits) == {"base", "architect-none", "impl-1", "docs-1"}, "both performers' commits landed"
    assert all(not forced for _, forced in remote.pushes), "no force push anywhere"
    assert [who for who, _ in remote.pushes] == ([first, "documenter" if first == "implementer" else "implementer"])

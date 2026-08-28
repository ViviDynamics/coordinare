"""Spec 151 (US3, T023 / SC-004) — one PR identity end-to-end: the node_id the
performer mints via the fake REST POST /pulls is the SAME record the coordinare's
check_mergeability / squash_merge act on, ending in a real local merge.
"""
from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from coordinare.bench.approver import gates_green
from coordinare.bench.fake_github_server import FakeGitHubServer
from coordinare.bench.fixtures import (
    apply_solution_branch,
    materialize_repo,
    seed_board,
    tiny_fixture,
)
from coordinare.services.fake_github import FakeGitHubService


async def test_opened_pr_is_the_merged_pr(tmp_path: Path) -> None:
    fx = tiny_fixture()
    bare = materialize_repo([fx], tmp_path / "repos")
    branch = fx.branch()
    apply_solution_branch(bare, branch, fx.solution_files, tmp_path / "sol")  # CI-green
    fake = FakeGitHubService(
        bare_repo_path=bare, human_reviewers=["reviewer1"],
        approver=gates_green, work_dir=tmp_path / "ci",
    )
    seed_board(fake, [fx])
    server = FakeGitHubServer(
        fake, bare_repo=bare, head_ref_index={branch: "PVTI_1"}, scratch=tmp_path
    )

    # Performer mints the PR through the REST boundary.
    async with TestClient(TestServer(server._build_app())) as client:
        resp = await client.post(
            "/repos/bench-org/bench-repo/pulls",
            json={"title": "t", "head": branch, "base": "main", "body": "Closes #1"},
        )
        node_id = (await resp.json())["node_id"]

    # It is a real FakeGitHubService record for the pushed branch (same identity).
    assert node_id == "PR_1"
    assert fake._prs[node_id]["head_ref"] == branch

    # The coordinare acts on THAT id: approve on green CI, then a real local merge.
    await fake.move_card("PVTI_1", "IN_REVIEW")
    mergeability = await fake.check_mergeability(node_id)
    assert mergeability["mergeable"] is True
    assert mergeability["review_decision"] == "APPROVED"

    result = await fake.squash_merge(node_id)
    assert result["merged"] is True
    assert result["id"] == node_id
    assert result["merge_commit"]["oid"], "expected a real merge commit oid"
    assert fake._prs[node_id]["merged"] is True

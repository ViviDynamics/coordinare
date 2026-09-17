"""Spec 151 (US2, T011) — the FakeGitHubServer REST handlers return the exact
shapes the performer's github.py parses, delegating all state to FakeGitHubService.

Deterministic + free: drives the aiohttp app directly (no git daemon, no model).
"""
from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from coordinare.bench.fake_github_server import FakeGitHubServer
from coordinare.bench.fixtures import (
    apply_solution_branch,
    materialize_repo,
    seed_board,
    tiny_fixture,
)
from coordinare.services.fake_github import FakeGitHubService


async def _boundary(tmp_path: Path):
    """Build a fake over a real bare repo with one pushed solution branch + PR."""
    fx = tiny_fixture()
    bare = materialize_repo([fx], tmp_path / "repos")
    branch = fx.branch()
    apply_solution_branch(bare, branch, fx.solution_files, tmp_path / "sol")
    fake = FakeGitHubService(
        bare_repo_path=bare, human_reviewers=["reviewer1"], work_dir=tmp_path / "ci",
    )
    seed_board(fake, [fx])
    server = FakeGitHubServer(
        fake, bare_repo=bare, head_ref_index={branch: "PVTI_1"}, scratch=tmp_path,
    )
    return fake, server, branch


async def test_host_vs_container_url_views(tmp_path: Path) -> None:
    """151 portability: host-facing URLs advertise 127.0.0.1; the performer-facing
    ones advertise host.docker.internal (same port) for a bridged container."""
    _fake, server, _branch = await _boundary(tmp_path)
    assert server.git_base_url.startswith("git://127.0.0.1:")
    assert server.rest_base_url.startswith("http://127.0.0.1:")
    port = server.git_base_url.rsplit(":", 1)[1]
    assert server.performer_git_base_url == f"git://host.docker.internal:{port}"
    assert server.performer_rest_base_url.startswith("http://host.docker.internal:")
    assert server.performer_graphql_url == server.performer_rest_base_url + "/graphql"


async def test_pulls_head_is_coordinare_branch_not_fixture_branch(tmp_path: Path) -> None:
    """Regression (real-mode 500): the /pulls head_ref_index the runner seeds must be
    keyed by the branch the COORDINARE pushes (make_branch_name from card id+title) —
    the head the real performer opens the PR with — not fx.branch() (the stub's
    solution-branch name). Under the old key, POST /pulls 500s 'no seeded card'."""
    from coordinare.workspace import make_branch_name

    fx = tiny_fixture()
    bare = materialize_repo([fx], tmp_path / "repos")
    fake = FakeGitHubService(
        bare_repo_path=bare, human_reviewers=["reviewer1"], work_dir=tmp_path / "ci",
    )
    seed_board(fake, [fx])  # card PVTI_1 with title=fx.title
    # Exactly the runner's seeding expression.
    fixtures_by_card = {"PVTI_1": fx}
    index = {make_branch_name(cid, f.title): cid for cid, f in fixtures_by_card.items()}
    coordinare_branch = make_branch_name("PVTI_1", fx.title)
    assert coordinare_branch in index and coordinare_branch != fx.branch()

    server = FakeGitHubServer(fake, bare_repo=bare, head_ref_index=index, scratch=tmp_path)
    async with TestClient(TestServer(server._build_app())) as client:
        ok = await client.post(
            "/repos/bench-org/bench-repo/pulls",
            json={"title": "t", "head": coordinare_branch, "base": "main", "body": "b"},
        )
        assert ok.status == 201
        # The old fx.branch() head is NOT seeded (the T025 keying bug). It must be
        # rejected — but with 422, not 500: performer/github.py special-cases only
        # 422 and raises GitHubAPIError on anything else, so a 500 killed the
        # dispatch opaquely. Also assert the GitHub-shaped `errors` list the
        # performer's 422 branch reads, and that no PR was minted.
        bad = await client.post(
            "/repos/bench-org/bench-repo/pulls",
            json={"title": "t", "head": fx.branch(), "base": "main", "body": "b"},
        )
        assert bad.status == 422
        assert (await bad.json())["errors"]
        assert len(fake._prs) == 1  # the bad head did not create a second PR


async def test_default_branch(tmp_path: Path) -> None:
    _fake, server, _branch = await _boundary(tmp_path)
    async with TestClient(TestServer(server._build_app())) as client:
        resp = await client.get("/repos/bench-org/bench-repo")
        assert resp.status == 200
        assert (await resp.json())["default_branch"] == "main"


async def test_create_pr_then_idempotent_422(tmp_path: Path) -> None:
    fake, server, branch = await _boundary(tmp_path)
    async with TestClient(TestServer(server._build_app())) as client:
        resp = await client.post(
            "/repos/bench-org/bench-repo/pulls",
            json={"title": "t", "head": branch, "base": "main", "body": "b"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["node_id"] == "PR_1"
        assert data["html_url"] == fake._prs["PR_1"]["url"]

        # Second create for the same head → 422 "already exists" (no duplicate mint).
        resp2 = await client.post(
            "/repos/bench-org/bench-repo/pulls",
            json={"title": "t", "head": branch, "base": "main", "body": "b"},
        )
        assert resp2.status == 422
        errs = (await resp2.json())["errors"]
        assert any("already exists" in e["message"] for e in errs)
        assert len(fake._prs) == 1  # still one PR


async def test_review_and_issue_comments_endpoints(tmp_path: Path) -> None:
    """Reviewing stage (real-mode 404 fix): the fake must serve the reviewer's two
    calls — GET /issues/{n}/comments (empty list) and POST /pulls/{n}/reviews
    (records the verdict, 201) — instead of aiohttp's default 404."""
    fake, server, branch = await _boundary(tmp_path)
    async with TestClient(TestServer(server._build_app())) as client:
        create = await client.post(
            "/repos/bench-org/bench-repo/pulls",
            json={"title": "t", "head": branch, "base": "main", "body": "b"},
        )
        assert create.status == 201

        comments = await client.get("/repos/bench-org/bench-repo/issues/1/comments")
        assert comments.status == 200
        assert await comments.json() == []

        # Security stage posts an advisory finding to the same endpoint (POST).
        posted = await client.post(
            "/repos/bench-org/bench-repo/issues/1/comments", json={"body": "finding"},
        )
        assert posted.status == 201
        assert "id" in await posted.json()

        review = await client.post(
            "/repos/bench-org/bench-repo/pulls/1/reviews",
            json={"event": "APPROVE", "body": "lgtm"},
        )
        assert review.status == 201
        # The verdict is recorded on the PR (event→state mapped) so review reads agree.
        recorded = await fake.get_pr_reviews("PR_1")
        assert [r["state"] for r in recorded] == ["APPROVED"]

        # Unknown PR number → 404, not a crash.
        missing = await client.post(
            "/repos/bench-org/bench-repo/pulls/999/reviews", json={"event": "COMMENT"},
        )
        assert missing.status == 404


async def test_existing_lookup_and_head_sha(tmp_path: Path) -> None:
    fake, server, branch = await _boundary(tmp_path)
    pr_id = fake.open_pr(issue_item_id="PVTI_1", head_ref=branch)
    head_sha = await fake._rev(branch)
    async with TestClient(TestServer(server._build_app())) as client:
        resp = await client.get(
            "/repos/bench-org/bench-repo/pulls",
            params={"head": f"bench-org:{branch}", "state": "open"},
        )
        arr = await resp.json()
        assert arr and arr[0]["node_id"] == pr_id

        pr_number = fake._prs[pr_id]["pr_number"]
        resp2 = await client.get(f"/repos/bench-org/bench-repo/pulls/{pr_number}")
        assert (await resp2.json())["head"]["sha"] == head_sha


async def test_check_runs_derived_from_real_pytest(tmp_path: Path) -> None:
    fake, server, branch = await _boundary(tmp_path)
    fake.open_pr(issue_item_id="PVTI_1", head_ref=branch)
    head_sha = await fake._rev(branch)
    async with TestClient(TestServer(server._build_app())) as client:
        resp = await client.get(
            f"/repos/bench-org/bench-repo/commits/{head_sha}/check-runs",
            params={"per_page": "100"},
        )
        runs = (await resp.json())["check_runs"]
        assert runs, "expected a pytest check run"
        run = runs[0]
        assert run["name"] == "pytest"
        assert run["status"] == "completed"
        # tiny_fixture's solution makes the acceptance test pass.
        assert run["conclusion"] == "success"


async def test_graphql_best_effort_empty(tmp_path: Path) -> None:
    _fake, server, _branch = await _boundary(tmp_path)
    async with TestClient(TestServer(server._build_app())) as client:
        resp = await client.post("/graphql", json={"query": "{ __typename }"})
        assert resp.status == 200
        nodes = (await resp.json())["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        assert nodes == []


async def test_pr_create_failure_surfaces_5xx(tmp_path: Path) -> None:
    fake, server, branch = await _boundary(tmp_path)

    def _boom(**_kwargs):
        raise RuntimeError("open_pr exploded")

    fake.open_pr = _boom  # type: ignore[method-assign]
    async with TestClient(TestServer(server._build_app())) as client:
        resp = await client.post(
            "/repos/bench-org/bench-repo/pulls",
            json={"title": "t", "head": branch, "base": "main", "body": "b"},
        )
        assert resp.status == 500
        assert "message" in (await resp.json())  # GitHubAPIError-shaped body

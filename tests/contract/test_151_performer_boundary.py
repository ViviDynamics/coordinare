"""Spec 151 (US2, T010) — CONTRACT: the FakeGitHubServer serves the exact shapes
the performer's real github.py parses. Drives the performer client (its own httpx)
against a live fake and asserts every call is answered locally, per
specs/151-real-performers/contracts/performer-facing-rest.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

from coordinare.bench.fake_github_server import FakeGitHubServer
from coordinare.bench.fixtures import (
    apply_solution_branch,
    materialize_repo,
    seed_board,
    tiny_fixture,
)
from coordinare.services.fake_github import FakeGitHubService

# The performer is a separate package under agent/performer/src.
_PERFORMER_SRC = Path(__file__).resolve().parents[2] / "agent" / "performer" / "src"
if str(_PERFORMER_SRC) not in sys.path:
    sys.path.insert(0, str(_PERFORMER_SRC))

pytestmark = pytest.mark.contract


async def test_performer_client_roundtrips_against_fake(tmp_path, monkeypatch) -> None:
    import performer.config as pc
    from performer import github as pgh
    from performer.models import Score

    fx = tiny_fixture()
    bare = materialize_repo([fx], tmp_path / "repos")
    branch = fx.branch()
    apply_solution_branch(bare, branch, fx.solution_files, tmp_path / "sol")  # CI-green
    fake = FakeGitHubService(bare_repo_path=bare, human_reviewers=["reviewer1"], work_dir=tmp_path / "ci")
    seed_board(fake, [fx])
    server = FakeGitHubServer(fake, bare_repo=bare, head_ref_index={branch: "PVTI_1"}, scratch=tmp_path)

    # A real listening REST server; point the performer's client at it (loopback).
    ts = TestServer(server._build_app())
    await ts.start_server()
    try:
        base = str(ts.make_url("")).rstrip("/")
        monkeypatch.setenv("GITHUB_API_URL", base)
        pc.get_settings.cache_clear()
        token = "fake-token"
        owner, repo = "bench-org", "bench-repo"

        # 3. default branch
        assert await pgh.get_default_branch(owner, repo, token) == "main"

        # 1. create pull request → (html_url, node_id)
        score = Score(title="t", repo_url="https://x/o/r.git", branch=branch, issue_number=1)
        html_url, node_id = await pgh.create_pull_request(owner, repo, score, branch, token)
        assert node_id == "PR_1"
        assert html_url == fake._prs["PR_1"]["url"]

        # 2a. existing PR lookup resolves the same record
        eu, en = await pgh.get_existing_pull_request(owner, repo, branch, token)
        assert (eu, en) == (html_url, node_id)

        # 2b. head sha
        pr_number = fake._prs["PR_1"]["pr_number"]
        head_sha = await pgh.get_pr_head_sha(owner, repo, pr_number, token)
        assert head_sha == await fake._rev(branch)
        assert len(head_sha) == 40

        # 4. check-runs derived from the real pytest result → classified "pass"
        runs = await pgh.get_check_runs(owner, repo, head_sha, token)
        verdict, failed = pgh.summarise_check_runs(runs)
        assert verdict == "pass", f"expected pass, got {verdict} ({runs})"
        assert failed == []
    finally:
        await ts.close()
        pc.get_settings.cache_clear()

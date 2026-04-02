"""Unit tests for performer.github."""
from __future__ import annotations

import pytest
import respx
import httpx

from performer.github import (
    GitHubAPIError,
    create_pull_request,
    get_check_runs,
    get_default_branch,
    get_existing_pull_request,
    post_pull_request_review,
    summarise_check_runs,
)
from performer.models import Score


def _score(**kwargs) -> Score:  # type: ignore[type-arg]
    defaults = dict(
        title="Add badge",
        description="Adds a CI badge",
        acceptance_criteria=["Badge visible in README"],
        repo_url="https://github.com/org/repo",
        branch="feat/badge",
        github_token="ghp_tok",
    )
    defaults.update(kwargs)
    return Score(**defaults)


class TestGetDefaultBranch:
    @respx.mock
    async def test_returns_default_branch(self) -> None:
        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        result = await get_default_branch("org", "repo", "tok")
        assert result == "main"

    @respx.mock
    async def test_raises_on_404(self) -> None:
        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with pytest.raises(GitHubAPIError) as exc_info:
            await get_default_branch("org", "repo", "tok")
        assert exc_info.value.status_code == 404


class TestCreatePullRequest:
    @respx.mock
    async def test_returns_html_url_and_node_id(self) -> None:
        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        respx.post("https://api.github.com/repos/org/repo/pulls").mock(
            return_value=httpx.Response(
                201,
                json={
                    "html_url": "https://github.com/org/repo/pull/42",
                    "node_id": "PR_node_123",
                },
            )
        )
        score = _score()
        html_url, node_id = await create_pull_request("org", "repo", score, score.branch, score.github_token)
        assert html_url == "https://github.com/org/repo/pull/42"
        assert node_id == "PR_node_123"

    @respx.mock
    async def test_uses_base_branch_from_score_when_set(self) -> None:
        respx.post("https://api.github.com/repos/org/repo/pulls").mock(
            return_value=httpx.Response(
                201,
                json={"html_url": "https://github.com/org/repo/pull/1", "node_id": "N1"},
            )
        )
        score = _score(base_branch="develop")
        await create_pull_request("org", "repo", score, score.branch, score.github_token)
        # If base_branch set, no default-branch API call should happen
        get_calls = [c for c in respx.calls if c.request.method == "GET"]
        assert len(get_calls) == 0

    @respx.mock
    async def test_raises_on_422(self) -> None:
        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        respx.post("https://api.github.com/repos/org/repo/pulls").mock(
            return_value=httpx.Response(422, json={"message": "Validation Failed"})
        )
        with pytest.raises(GitHubAPIError) as exc_info:
            await create_pull_request("org", "repo", _score(), "feat/x", "tok")
        assert exc_info.value.status_code == 422

    @respx.mock
    async def test_422_already_exists_returns_existing_pr(self) -> None:
        """422 with 'already exists' error fetches and returns the existing PR."""
        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        respx.post("https://api.github.com/repos/org/repo/pulls").mock(
            return_value=httpx.Response(
                422,
                json={"errors": [{"message": "A pull request already exists for org:feat/badge"}]},
            )
        )
        respx.get(
            "https://api.github.com/repos/org/repo/pulls",
            params={"head": "org:feat/badge", "state": "open"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[{"html_url": "https://github.com/org/repo/pull/7", "node_id": "PR_existing"}],
            )
        )
        score = _score()
        html_url, node_id = await create_pull_request("org", "repo", score, score.branch, score.github_token)
        assert html_url == "https://github.com/org/repo/pull/7"
        assert node_id == "PR_existing"


class TestGetExistingPullRequest:
    @respx.mock
    async def test_returns_html_url_and_node_id(self) -> None:
        respx.get(
            "https://api.github.com/repos/org/repo/pulls",
            params={"head": "org:feat/x", "state": "open"},
        ).mock(
            return_value=httpx.Response(
                200,
                json=[{"html_url": "https://github.com/org/repo/pull/5", "node_id": "PR_5"}],
            )
        )
        html_url, node_id = await get_existing_pull_request("org", "repo", "feat/x", "tok")
        assert html_url == "https://github.com/org/repo/pull/5"
        assert node_id == "PR_5"

    @respx.mock
    async def test_raises_404_when_no_open_pr(self) -> None:
        respx.get(
            "https://api.github.com/repos/org/repo/pulls",
            params={"head": "org:feat/x", "state": "open"},
        ).mock(
            return_value=httpx.Response(200, json=[])
        )
        with pytest.raises(GitHubAPIError) as exc_info:
            await get_existing_pull_request("org", "repo", "feat/x", "tok")
        assert exc_info.value.status_code == 404

    @respx.mock
    async def test_raises_on_api_error(self) -> None:
        respx.get(
            "https://api.github.com/repos/org/repo/pulls",
            params={"head": "org:feat/x", "state": "open"},
        ).mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )
        with pytest.raises(GitHubAPIError) as exc_info:
            await get_existing_pull_request("org", "repo", "feat/x", "tok")
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# US5: get_check_runs (T027-T028)
# ---------------------------------------------------------------------------


class TestRequireToken:
    async def test_get_check_runs_empty_token_raises_401(self) -> None:
        with pytest.raises(GitHubAPIError) as exc_info:
            await get_check_runs("org", "repo", "abc123", "")
        assert exc_info.value.status_code == 401

    async def test_get_check_runs_whitespace_token_raises_401(self) -> None:
        with pytest.raises(GitHubAPIError) as exc_info:
            await get_check_runs("org", "repo", "abc123", "   ")
        assert exc_info.value.status_code == 401

    async def test_get_default_branch_empty_token_raises_401(self) -> None:
        with pytest.raises(GitHubAPIError) as exc_info:
            await get_default_branch("org", "repo", "")
        assert exc_info.value.status_code == 401

    @respx.mock
    async def test_create_pull_request_empty_token_raises_401(self) -> None:
        with pytest.raises(GitHubAPIError) as exc_info:
            await create_pull_request("org", "repo", _score(), "feat/x", "")
        assert exc_info.value.status_code == 401


class TestGetCheckRuns:
    @respx.mock
    async def test_returns_check_runs_list(self) -> None:
        check_runs = [
            {"id": 1, "name": "build", "status": "completed", "conclusion": "success", "output": {}}
        ]
        respx.get("https://api.github.com/repos/org/repo/commits/abc123/check-runs").mock(
            return_value=httpx.Response(200, json={"check_runs": check_runs})
        )
        result = await get_check_runs("org", "repo", "abc123", "tok")
        assert result == check_runs

    @respx.mock
    async def test_raises_on_non_200(self) -> None:
        respx.get("https://api.github.com/repos/org/repo/commits/abc123/check-runs").mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )
        with pytest.raises(GitHubAPIError) as exc_info:
            await get_check_runs("org", "repo", "abc123", "tok")
        assert exc_info.value.status_code == 403

    @respx.mock
    async def test_empty_check_runs_returns_empty_list(self) -> None:
        respx.get("https://api.github.com/repos/org/repo/commits/sha/check-runs").mock(
            return_value=httpx.Response(200, json={"check_runs": []})
        )
        result = await get_check_runs("org", "repo", "sha", "tok")
        assert result == []


# ---------------------------------------------------------------------------
# US5: summarise_check_runs (T029-T033)
# ---------------------------------------------------------------------------


class TestSummariseCheckRuns:
    def test_all_success_returns_pass(self) -> None:
        runs = [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "neutral"},
        ]
        verdict, failed = summarise_check_runs(runs)
        assert verdict == "pass"
        assert failed == []

    def test_skipped_counts_as_pass(self) -> None:
        runs = [{"name": "optional", "status": "completed", "conclusion": "skipped"}]
        verdict, failed = summarise_check_runs(runs)
        assert verdict == "pass"

    def test_any_failure_returns_fail(self) -> None:
        runs = [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "tests", "status": "completed", "conclusion": "failure"},
        ]
        verdict, failed = summarise_check_runs(runs)
        assert verdict == "fail"
        assert len(failed) == 1
        assert failed[0]["name"] == "tests"

    def test_in_progress_with_no_failures_returns_pending(self) -> None:
        runs = [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "deploy", "status": "in_progress", "conclusion": None},
        ]
        verdict, failed = summarise_check_runs(runs)
        assert verdict == "pending"
        assert failed == []

    def test_empty_list_returns_pass(self) -> None:
        verdict, failed = summarise_check_runs([])
        assert verdict == "pass"
        assert failed == []

    def test_action_required_treated_as_failure(self) -> None:
        runs = [{"name": "security", "status": "completed", "conclusion": "action_required"}]
        verdict, failed = summarise_check_runs(runs)
        assert verdict == "fail"
        assert failed[0]["name"] == "security"

    def test_timed_out_treated_as_failure(self) -> None:
        runs = [{"name": "integration", "status": "completed", "conclusion": "timed_out"}]
        verdict, _ = summarise_check_runs(runs)
        assert verdict == "fail"

    def test_cancelled_treated_as_failure(self) -> None:
        runs = [{"name": "e2e", "status": "completed", "conclusion": "cancelled"}]
        verdict, _ = summarise_check_runs(runs)
        assert verdict == "fail"

    def test_unknown_conclusion_treated_as_pending(self) -> None:
        """Completed runs with unrecognised conclusions are treated as pending, not passing."""
        runs = [
            {"name": "build", "status": "completed", "conclusion": "unknown_future_value"},
            {"name": "lint", "status": "completed", "conclusion": None},
        ]
        verdict, failed = summarise_check_runs(runs)
        assert verdict == "pending"
        assert failed == []

    def test_failure_takes_precedence_over_pending(self) -> None:
        """If both a failure and an in_progress run exist, verdict is fail not pending."""
        runs = [
            {"name": "tests", "status": "completed", "conclusion": "failure"},
            {"name": "build", "status": "in_progress", "conclusion": None},
        ]
        verdict, failed = summarise_check_runs(runs)
        assert verdict == "fail"
        assert any(r["name"] == "tests" for r in failed)


# ---------------------------------------------------------------------------
# post_pull_request_review
# ---------------------------------------------------------------------------


_REVIEW_URL = "https://api.github.com/repos/org/repo/pulls/42/reviews"


# ---------------------------------------------------------------------------
# 036 — GitHub Enterprise Support: configurable API URL
# ---------------------------------------------------------------------------


class TestGitHubAPIURLConfigurable:
    """Verify performer github module uses the configured GITHUB_API_URL."""

    @respx.mock
    async def test_get_default_branch_uses_configured_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from performer.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("GITHUB_API_URL", "https://ghes.example.com/api/v3")
        respx.get("https://ghes.example.com/api/v3/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        result = await get_default_branch("org", "repo", "tok")
        assert result == "main"
        get_settings.cache_clear()

    @respx.mock
    async def test_get_check_runs_uses_configured_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from performer.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("GITHUB_API_URL", "https://ghes.example.com/api/v3")
        respx.get("https://ghes.example.com/api/v3/repos/org/repo/commits/abc/check-runs").mock(
            return_value=httpx.Response(200, json={"check_runs": []})
        )
        result = await get_check_runs("org", "repo", "abc", "tok")
        assert result == []
        get_settings.cache_clear()

    @respx.mock
    async def test_default_url_backward_compatible(self) -> None:
        """Without env var override, calls go to api.github.com (backward compat)."""
        from performer.config import get_settings
        get_settings.cache_clear()
        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        result = await get_default_branch("org", "repo", "tok")
        assert result == "main"
        get_settings.cache_clear()


class TestPostPullRequestReview:
    @respx.mock
    async def test_post_review_approve_sends_correct_payload(self) -> None:
        """APPROVE event sends event + body, no comments key in payload."""
        route = respx.post(_REVIEW_URL).mock(
            return_value=httpx.Response(200, json={"id": 1, "state": "APPROVED"})
        )
        result = await post_pull_request_review(
            owner="org",
            repo="repo",
            pr_number=42,
            event="APPROVE",
            body="Looks good!",
            comments=[],
            token="tok",
        )
        assert result == {"id": 1, "state": "APPROVED"}
        # Verify the payload sent to GitHub
        sent = route.calls[0].request
        import json as _json

        payload = _json.loads(sent.content)
        assert payload["event"] == "APPROVE"
        assert payload["body"] == "Looks good!"
        # Empty comments should NOT produce a "comments" key in the payload
        assert "comments" not in payload

    @respx.mock
    async def test_post_review_request_changes_includes_comments(self) -> None:
        """REQUEST_CHANGES event includes inline comments with path, line, body."""
        route = respx.post(_REVIEW_URL).mock(
            return_value=httpx.Response(
                200, json={"id": 2, "state": "CHANGES_REQUESTED"}
            )
        )
        inline_comments = [
            {"path": "src/main.py", "line": 10, "body": "Fix this"},
            {"path": "src/util.py", "line": 25, "body": "Rename variable"},
        ]
        result = await post_pull_request_review(
            owner="org",
            repo="repo",
            pr_number=42,
            event="REQUEST_CHANGES",
            body="Needs work",
            comments=inline_comments,
            token="tok",
        )
        assert result["state"] == "CHANGES_REQUESTED"
        import json as _json

        payload = _json.loads(route.calls[0].request.content)
        assert payload["event"] == "REQUEST_CHANGES"
        assert payload["body"] == "Needs work"
        assert len(payload["comments"]) == 2
        assert payload["comments"][0] == {
            "path": "src/main.py",
            "line": 10,
            "body": "Fix this",
        }
        assert payload["comments"][1] == {
            "path": "src/util.py",
            "line": 25,
            "body": "Rename variable",
        }

    @respx.mock
    async def test_post_review_non_2xx_raises_github_api_error(self) -> None:
        """Non-2xx response (422) raises GitHubAPIError."""
        respx.post(_REVIEW_URL).mock(
            return_value=httpx.Response(
                422, json={"message": "Validation Failed"}
            )
        )
        with pytest.raises(GitHubAPIError) as exc_info:
            await post_pull_request_review(
                owner="org",
                repo="repo",
                pr_number=42,
                event="APPROVE",
                body="LGTM",
                comments=[],
                token="tok",
            )
        assert exc_info.value.status_code == 422

    @respx.mock
    async def test_post_review_empty_comments_is_valid(self) -> None:
        """An empty comments list is accepted and the call succeeds."""
        respx.post(_REVIEW_URL).mock(
            return_value=httpx.Response(200, json={"id": 3, "state": "COMMENTED"})
        )
        result = await post_pull_request_review(
            owner="org",
            repo="repo",
            pr_number=42,
            event="COMMENT",
            body="Informational note",
            comments=[],
            token="tok",
        )
        assert result["id"] == 3
        assert result["state"] == "COMMENTED"

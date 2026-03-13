"""Unit tests for performer.github."""
from __future__ import annotations

import pytest
import respx
import httpx

from performer.github import GitHubAPIError, create_pull_request, get_default_branch, get_existing_pull_request
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

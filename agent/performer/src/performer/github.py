"""GitHub API client — PR creation, default branch detection, and check-run polling."""
from __future__ import annotations

from typing import Literal

import httpx
import structlog

from performer.models import Score

log = structlog.get_logger(__name__)

_GITHUB_API = "https://api.github.com"


class GitHubAPIError(RuntimeError):
    """Raised when the GitHub API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"GitHub API error {status_code}: {message}")
        self.status_code = status_code


def _pr_body(score: Score) -> str:
    parts = [score.description] if score.description else []
    if score.acceptance_criteria:
        parts.append("\n## Acceptance Criteria\n")
        parts.extend(f"- {c}" for c in score.acceptance_criteria)
    return "\n".join(parts)


async def get_default_branch(owner: str, repo: str, token: str) -> str:
    """Return the repository's default branch name."""
    _require_token(token, "get_default_branch")
    url = f"{_GITHUB_API}/repos/{owner}/{repo}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise GitHubAPIError(resp.status_code, resp.text)
    return resp.json()["default_branch"]


async def get_existing_pull_request(
    owner: str,
    repo: str,
    branch: str,
    token: str,
) -> tuple[str, str]:
    """Return ``(html_url, node_id)`` for an existing open PR on *branch*."""
    _require_token(token, "get_existing_pull_request")
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/pulls"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers, params={"head": f"{owner}:{branch}", "state": "open"})
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    prs = resp.json()
    if not prs:
        raise GitHubAPIError(404, f"No open PR found for branch {branch!r}")
    data = prs[0]
    return data["html_url"], data["node_id"]


_FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required"})
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


def _require_token(token: str, operation: str) -> None:
    """Raise GitHubAPIError immediately when *token* is empty or whitespace-only.

    Prevents sending a malformed ``Authorization: Bearer `` or
    ``Authorization: Bearer    `` header to GitHub, which would result in a 401
    that might be silently retried.
    """
    if not token.strip():
        raise GitHubAPIError(401, f"github_token is required for {operation}")


async def get_check_runs(owner: str, repo: str, ref: str, token: str) -> list[dict]:  # type: ignore[type-arg]
    """Return all check runs for a commit *ref* via the GitHub Checks API."""
    _require_token(token, "get_check_runs")
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/commits/{ref}/check-runs"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers, params={"per_page": "100"})
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    return resp.json().get("check_runs", [])


def summarise_check_runs(
    check_runs: list[dict],  # type: ignore[type-arg]
) -> tuple[Literal["pass", "fail", "pending"], list[dict]]:  # type: ignore[type-arg]
    """Classify check runs and return (verdict, failed_runs).

    - "pass":    all completed runs have a passing conclusion (or list is empty)
    - "fail":    one or more runs completed with a failing conclusion
    - "pending": no failures yet, but some runs are still queued or in_progress
    """
    failed: list[dict] = []  # type: ignore[type-arg]
    has_pending = False
    for run in check_runs:
        conclusion = run.get("conclusion")
        status = run.get("status", "")
        if conclusion in _FAILING_CONCLUSIONS:
            failed.append(run)
        elif status in ("queued", "in_progress"):
            has_pending = True
        elif status == "completed" and conclusion not in _PASSING_CONCLUSIONS:
            # Unknown or null conclusion — treat conservatively as pending
            has_pending = True
    if failed:
        return "fail", failed
    if has_pending:
        return "pending", []
    return "pass", []


async def create_pull_request(
    owner: str,
    repo: str,
    score: Score,
    branch: str,
    token: str,
) -> tuple[str, str]:
    """Open a pull request and return ``(html_url, node_id)``.

    If a PR already exists for *branch*, returns the existing PR's details
    rather than raising an error.
    """
    _require_token(token, "create_pull_request")
    base = score.base_branch or await get_default_branch(owner, repo, token)
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/pulls"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    body = {
        "title": score.title,
        "head": branch,
        "base": base,
        "body": _pr_body(score),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body, headers=headers)
    if resp.status_code == 422:
        # PR already exists — fetch and return it instead of erroring.
        error_data = resp.json()
        errors = error_data.get("errors", [])
        if any("already exists" in (e.get("message") or "") for e in errors):
            log.info("pull request already exists, fetching existing PR", branch=branch)
            return await get_existing_pull_request(owner, repo, branch, token)
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    data = resp.json()
    html_url: str = data["html_url"]
    node_id: str = data["node_id"]
    log.info("pull request opened", pr_url=html_url)
    return html_url, node_id


async def post_pull_request_review(
    owner: str,
    repo: str,
    pr_number: int,
    event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
    body: str,
    comments: list[dict],  # type: ignore[type-arg]
    token: str,
) -> dict:  # type: ignore[type-arg]
    """Post a review to a GitHub Pull Request via the Reviews API.

    Parameters
    ----------
    owner, repo : str
        Repository coordinates.
    pr_number : int
        Pull request number.
    event : Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
        Review action.
    body : str
        Top-level review comment body.
    comments : list[dict]
        Inline review comments, each with ``path``, ``line``, and ``body``.
    token : str
        GitHub token for authentication.

    Returns
    -------
    dict
        The created review object from the GitHub API.

    Raises
    ------
    GitHubAPIError
        On non-2xx response.
    """
    _require_token(token, "post_pull_request_review")
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    payload: dict = {  # type: ignore[type-arg]
        "event": event,
        "body": body,
    }
    if comments:
        valid_comments = []
        for c in comments:
            if not isinstance(c, dict):
                continue
            path = c.get("file", c.get("path", ""))
            body = c.get("body", "")
            if path and body:
                valid_comments.append({"path": path, "line": c.get("line", 1), "body": body})
        if valid_comments:
            payload["comments"] = valid_comments
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    log.info("review posted", owner=owner, repo=repo, pr_number=pr_number, review_event=event)
    return resp.json()

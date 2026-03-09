"""GitHub API client — PR creation and default branch detection."""
from __future__ import annotations

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
    url = f"{_GITHUB_API}/repos/{owner}/{repo}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise GitHubAPIError(resp.status_code, resp.text)
    return resp.json()["default_branch"]


async def create_pull_request(
    owner: str,
    repo: str,
    score: Score,
    branch: str,
    token: str,
) -> tuple[str, str]:
    """Open a pull request and return ``(html_url, node_id)``."""
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
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    data = resp.json()
    html_url: str = data["html_url"]
    node_id: str = data["node_id"]
    log.info("pull request opened", pr_url=html_url)
    return html_url, node_id

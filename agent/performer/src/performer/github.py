"""GitHub API client — PR creation, default branch detection, and check-run polling."""
from __future__ import annotations

from typing import Literal

import httpx
import structlog

from performer.config import get_settings
from performer.models import Score

log = structlog.get_logger(__name__)


def _github_api() -> str:
    """Return the GitHub REST API base URL from performer settings."""
    return get_settings().GITHUB_API_URL.rstrip("/")


class GitHubAPIError(RuntimeError):
    """Raised when the GitHub API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"GitHub API error {status_code}: {message}")
        self.status_code = status_code


def _pr_body(score: Score) -> str:
    parts = []
    # Link to the issue so GitHub auto-links the PR to the project board
    if score.issue_number:
        owner, repo = score.owner_repo
        parts.append(f"Closes #{score.issue_number}")
        parts.append("")
    if score.description:
        parts.append(score.description)
    if score.acceptance_criteria:
        parts.append("\n## Acceptance Criteria\n")
        parts.extend(f"- {c}" for c in score.acceptance_criteria)
    return "\n".join(parts)


async def get_default_branch(owner: str, repo: str, token: str) -> str:
    """Return the repository's default branch name."""
    _require_token(token, "get_default_branch")
    url = f"{_github_api()}/repos/{owner}/{repo}"
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
    url = f"{_github_api()}/repos/{owner}/{repo}/pulls"
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


async def get_pr_head_sha(owner: str, repo: str, pr_number: int, token: str) -> str:
    """Return the head SHA of an open or closed PR."""
    _require_token(token, "get_pr_head_sha")
    url = f"{_github_api()}/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    sha = resp.json().get("head", {}).get("sha")
    if not sha:
        raise GitHubAPIError(500, f"PR {pr_number} has no head.sha")
    return sha


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
    url = f"{_github_api()}/repos/{owner}/{repo}/commits/{ref}/check-runs"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers, params={"per_page": "100"})
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    return resp.json().get("check_runs", [])


async def get_check_run_logs(
    owner: str,
    repo: str,
    job_id: int,
    token: str,
    *,
    max_chars: int = 4000,
) -> str:
    """Return the tail of a GitHub Actions job's log, or "" if unavailable.

    For a GitHub Actions check run, ``check_run.id`` is the Actions job id.
    The logs endpoint returns a 302 redirect to a pre-signed URL — we follow
    it without sending our Authorization header (S3 rejects it).

    Returns at most *max_chars* characters from the end of the log so we
    surface the actual failure (which is almost always near the bottom).
    Never raises — log retrieval is best-effort context for the model.
    """
    if not token.strip():
        return ""
    url = f"{_github_api()}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    return ""
                resp = await client.get(location)
            if not resp.is_success:
                return ""
            text = resp.text
    except Exception as exc:
        log.warning("get_check_run_logs.failed", job_id=job_id, error=str(exc))
        return ""
    if len(text) > max_chars:
        return "... (log truncated) ...\n" + text[-max_chars:]
    return text


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
    url = f"{_github_api()}/repos/{owner}/{repo}/pulls"
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
        # PR already exists — fetch it and update body/title to keep linkage current.
        error_data = resp.json()
        errors = error_data.get("errors", [])
        if any("already exists" in (e.get("message") or "") for e in errors):
            log.info("pull request already exists, updating and fetching", branch=branch)
            html_url, node_id = await get_existing_pull_request(owner, repo, branch, token)
            # Update the existing PR body so issue linkage and description stay current
            pr_number = html_url.rstrip("/").rsplit("/", 1)[-1]
            update_url = f"{_github_api()}/repos/{owner}/{repo}/pulls/{pr_number}"
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    await client.patch(update_url, json={"body": _pr_body(score)}, headers=headers)
            except Exception as exc:
                log.warning("update_existing_pr_body_failed", error=str(exc))
            return html_url, node_id
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
    url = f"{_github_api()}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
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


async def post_pr_comment(
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    token: str,
) -> dict:  # type: ignore[type-arg]
    """Post a general comment to a GitHub Pull Request (issue comments API).

    Used for advisory security findings and other non-inline comments.
    POST /repos/{owner}/{repo}/issues/{pr_number}/comments
    """
    _require_token(token, "post_pr_comment")
    url = f"{_github_api()}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json={"body": body})
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    log.info("pr comment posted", owner=owner, repo=repo, pr_number=pr_number)
    return resp.json()


async def list_pr_comments(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
) -> list[dict]:  # type: ignore[type-arg]
    """List general (issue-style) comments on a Pull Request.

    Used by the security advisory dedup path to avoid reposting findings
    already present on the PR. Returns the first page only (100 comments),
    which is sufficient for the dedup use case — older duplicates beyond
    that page would have already been deduplicated against on prior cycles.
    GET /repos/{owner}/{repo}/issues/{pr_number}/comments
    """
    _require_token(token, "list_pr_comments")
    url = f"{_github_api()}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers, params={"per_page": 100})
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    data = resp.json()
    return data if isinstance(data, list) else []


async def post_issue_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    token: str,
) -> dict:  # type: ignore[type-arg]
    """Post a general comment to a GitHub issue card.

    Uses the same issues comments API endpoint as PR comments:
    POST /repos/{owner}/{repo}/issues/{issue_number}/comments
    """
    _require_token(token, "post_issue_comment")
    url = f"{_github_api()}/repos/{owner}/{repo}/issues/{issue_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json={"body": body})
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text)
    log.info("issue comment posted", owner=owner, repo=repo, issue_number=issue_number)
    return resp.json()


async def resolve_pr_review_threads(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
) -> int:
    """Resolve all unresolved review threads on a PR, regardless of comment author.

    Called by the reviewer after verifying that implementer fixes address
    the feedback. Outdated threads (where the referenced code has changed)
    are treated identically to live threads. Threads from any author —
    humans, the coordinare bot, or other bots like Copilot — are all closed.

    Returns the number of threads successfully resolved. GraphQL errors are
    logged with the thread ID and first comment author so silent failures
    are visible in operations.
    """
    _require_token(token, "resolve_pr_review_threads")
    # 151: configurable so the board-sim bench routes this to the loopback fake
    # instead of api.github.com (best-effort call; default remains real GitHub).
    graphql_url = get_settings().GITHUB_GRAPHQL_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    query = """
    query($owner: String!, $repo: String!, $pr: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $pr) {
          reviewThreads(first: 100) {
            nodes {
              id
              isResolved
              isOutdated
              comments(first: 1) { nodes { author { login } } }
            }
          }
        }
      }
    }
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(graphql_url, headers=headers, json={
            "query": query,
            "variables": {"owner": owner, "repo": repo, "pr": pr_number},
        })
    if not resp.is_success:
        log.warning("resolve_threads.fetch_failed", status=resp.status_code, body=resp.text[:200])
        return 0

    payload = resp.json()
    if payload.get("errors"):
        log.warning("resolve_threads.fetch_graphql_errors", errors=payload["errors"])
        return 0

    threads = (
        payload.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    unresolved = [t for t in threads if not t.get("isResolved")]
    if not unresolved:
        return 0

    def _first_author(thread: dict) -> str:  # type: ignore[type-arg]
        nodes = (thread.get("comments") or {}).get("nodes") or []
        if nodes and isinstance(nodes[0], dict):
            author = nodes[0].get("author") or {}
            return str(author.get("login", "unknown"))
        return "unknown"

    resolved = 0
    failed: list[dict] = []  # type: ignore[type-arg]
    mutation = (
        "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) "
        "{ thread { id isResolved } } }"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        for thread in unresolved:
            tid = thread.get("id", "")
            author = _first_author(thread)
            outdated = bool(thread.get("isOutdated"))
            try:
                resp = await client.post(graphql_url, headers=headers, json={
                    "query": mutation,
                    "variables": {"id": tid},
                })
            except Exception as exc:
                failed.append({"id": tid, "author": author, "outdated": outdated, "error": str(exc)})
                continue

            if not resp.is_success:
                failed.append({
                    "id": tid, "author": author, "outdated": outdated,
                    "status": resp.status_code, "body": resp.text[:200],
                })
                continue

            try:
                body = resp.json()
            except Exception as exc:
                failed.append({"id": tid, "author": author, "outdated": outdated, "error": f"non-json: {exc}"})
                continue

            errors = body.get("errors") or []
            thread_data = ((body.get("data") or {}).get("resolveReviewThread") or {}).get("thread") or {}
            if errors or not thread_data.get("isResolved"):
                failed.append({
                    "id": tid, "author": author, "outdated": outdated,
                    "errors": errors, "thread": thread_data,
                })
                continue

            resolved += 1

    log.info(
        "resolve_threads.done",
        owner=owner, repo=repo, pr_number=pr_number,
        attempted=len(unresolved), resolved=resolved, failed=len(failed),
    )
    if failed:
        log.warning(
            "resolve_threads.partial_failure",
            pr_number=pr_number, failed=failed,
        )
    return resolved


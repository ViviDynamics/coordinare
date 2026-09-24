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
_LIST_PER_PAGE = 100
_LIST_MAX_PAGES = 10


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
    runs: list[dict] = []  # type: ignore[type-arg]
    async with httpx.AsyncClient(timeout=30) as client:
        page = 1
        while True:
            resp = await client.get(url, headers=headers, params={"per_page": str(_LIST_PER_PAGE), "page": str(page)})
            if not resp.is_success:
                raise GitHubAPIError(resp.status_code, resp.text)
            batch = resp.json().get("check_runs", [])
            runs.extend(batch)
            if len(batch) < _LIST_PER_PAGE or page >= _LIST_MAX_PAGES:
                break
            page += 1
    from performer.ci_evidence import fetch_failure_evidence

    for run in runs:
        if run.get("conclusion") not in {"failure", "startup_failure", "action_required", "timed_out"}:
            continue
        evidence = await fetch_failure_evidence(_github_api(), token, owner, repo, int(run.get("id") or 0), run.get("details_url"))
        messages = [*evidence["annotations"], *evidence["failed_steps"]]
        run["setup_failure"] = evidence["setup_failure"]
        run["evidence_access_denied"] = evidence.get("access_denied", False)
        if messages:
            output = dict(run.get("output") or {})
            output["summary"] = "\n".join([str(output.get("summary") or ""), *messages])
            run["output"] = output
    return runs


async def get_commit_statuses(owner: str, repo: str, ref: str, token: str) -> list[dict]:  # type: ignore[type-arg]
    """Return the commit statuses for *ref* via the REST commit-status API.

    Repos whose CI is a plain status context (Jenkins, older CircleCI) carry
    no check runs at all, so the CI gate folds these in as pseudo runs.
    Paginated like the check-run listing: the combined `/status` view caps
    its ``statuses`` array at 30, and a failing context beyond the first page
    must not slip past the gate — so this walks the history endpoint.
    """
    _require_token(token, "get_commit_statuses")
    url = f"{_github_api()}/repos/{owner}/{repo}/statuses/{ref}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, _LIST_MAX_PAGES + 1):
            resp = await client.get(url, headers=headers, params={"per_page": str(_LIST_PER_PAGE), "page": str(page)})
            if not resp.is_success:
                raise GitHubAPIError(resp.status_code, resp.text)
            batch = resp.json()
            out.extend(batch)
            if len(batch) < _LIST_PER_PAGE:
                break
    return out


async def fetch_pr_review_context(owner: str, repo: str, pr_number: int, token: str) -> dict:  # type: ignore[type-arg]
    """The review facts a closer must not guess: PR author, reviewDecision,
    and the logins behind any CHANGES_REQUESTED review, bots excluded.

    A repo whose CI is green but whose human reviewer hit "Request changes"
    keeps the verdict changes_requested; a bot's CHANGES_REQUESTED does not
    outrank the work.
    """
    _require_token(token, "fetch_pr_review_context")
    base = f"{_github_api()}/repos/{owner}/{repo}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as client:
        pr_resp = await client.get(f"{base}/pulls/{pr_number}", headers=headers)
        if not pr_resp.is_success:
            raise GitHubAPIError(pr_resp.status_code, pr_resp.text[:200])
        pr = pr_resp.json()
        reviews: list[dict] = []
        page = 1
        while True:
            resp = await client.get(f"{base}/pulls/{pr_number}/reviews", headers=headers, params={"per_page": str(_LIST_PER_PAGE), "page": str(page)})
            if not resp.is_success:
                raise GitHubAPIError(resp.status_code, resp.text[:200])
            batch = resp.json()
            reviews.extend(batch)
            if len(batch) < _LIST_PER_PAGE or page >= _LIST_MAX_PAGES:
                break
            page += 1
    changes_requested_humans = [
        login
        for review in reviews
        if review.get("state") == "CHANGES_REQUESTED"
        for login in [str((review.get("user") or {}).get("login", "") or "")]
        if login and not login.endswith("[bot]")
    ]
    return {
        "pr_author": str((pr.get("user") or {}).get("login", "") or ""),
        "review_decision": str(pr.get("review_decision") or ""),
        "changes_requested_humans": changes_requested_humans,
    }


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



def _comment_dict(comment_node: dict) -> dict:  # type: ignore[type-arg]
    """One review comment, with a null author normalised to an empty login."""
    author = comment_node.get("author") or {}
    return {
        "author": str(author.get("login", "") or ""),
        "body": comment_node.get("body", "") or "",
        "created_at": comment_node.get("createdAt", "") or "",
        "author_association": str(comment_node.get("authorAssociation") or "NONE"),
    }


async def _fetch_thread_comments(client, graphql_url: str, headers: dict, thread_id: str, cursor: str | None, max_pages: int) -> list[dict]:  # type: ignore[type-arg]
    """The comments of one thread beyond the first page, in order."""
    query = """
    query($id: ID!, $cursor: String) {
      node(id: $id) {
        ... on PullRequestReviewThread {
          comments(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes { author { login } authorAssociation body createdAt }
          }
        }
      }
    }
    """
    out: list[dict] = []  # type: ignore[type-arg]
    for _page in range(max_pages):
        if not cursor:
            break
        resp = await client.post(graphql_url, headers=headers, json={"query": query, "variables": {"id": thread_id, "cursor": cursor}})
        if not resp.is_success:
            log.warning("fetch_review_threads.comment_page_failed", thread=thread_id, status=resp.status_code)
            break
        payload = resp.json()
        if payload.get("errors"):
            log.warning("fetch_review_threads.comment_page_errors", thread=thread_id, errors=payload["errors"])
            break
        comments = ((payload.get("data") or {}).get("node") or {}).get("comments") or {}
        out.extend(_comment_dict(c) for c in comments.get("nodes", []) or [])
        page_info = comments.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return out


async def fetch_review_threads(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
    *,
    max_pages: int = 5,
) -> tuple[list[dict], int]:
    """Fetch all review threads on a PR with full comment details and pagination.

    Returns a tuple of (threads, pages_read). Each thread dict contains:
    - id, path, line, resolved, outdated, comments (list of dicts with author, body, created_at)

    Paginates through threads until exhausted or max_pages is reached.
    Raises GitHubAPIError on non-success response or GraphQL errors.
    """
    _require_token(token, "fetch_review_threads")
    graphql_url = get_settings().GITHUB_GRAPHQL_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    query = """
    query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $pr) {
          reviewThreads(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              isResolved
              isOutdated
              path
              line
              comments(first: 100) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  author { login }
                  authorAssociation
                  body
                  createdAt
                }
              }
            }
          }
        }
      }
    }
    """

    threads: list[dict] = []
    pages_read = 0
    cursor = None
    async with httpx.AsyncClient(timeout=30) as client:
        while pages_read < max_pages:
            resp = await client.post(graphql_url, headers=headers, json={
                "query": query,
                "variables": {"owner": owner, "repo": repo, "pr": pr_number, "cursor": cursor},
            })
            if not resp.is_success:
                raise GitHubAPIError(resp.status_code, resp.text[:200])

            payload = resp.json()
            if payload.get("errors"):
                raise GitHubAPIError(200, str(payload["errors"]))

            thread_data = (
                payload.get("data", {})
                .get("repository", {})
                .get("pullRequest", {})
                .get("reviewThreads", {})
            )
            nodes = thread_data.get("nodes", [])
            page_info = thread_data.get("pageInfo", {})

            # Convert GraphQL response to thread dicts
            for node in nodes:
                comments_list = [_comment_dict(c) for c in (node.get("comments", {}) or {}).get("nodes", [])]
                comment_pages = (node.get("comments", {}) or {}).get("pageInfo", {}) or {}
                if comment_pages.get("hasNextPage"):
                    # A thread with more than 100 comments: page the rest, so the
                    # closer's classification sees the real last comment (FR-002).
                    comments_list.extend(
                        await _fetch_thread_comments(client, graphql_url, headers, node.get("id", ""), comment_pages.get("endCursor"), max_pages),
                    )
                threads.append({
                    "id": node.get("id", ""),
                    "path": node.get("path", ""),
                    "line": node.get("line") or 0,
                    "resolved": bool(node.get("isResolved")),
                    "outdated": bool(node.get("isOutdated")),
                    "comments": comments_list,
                })

            pages_read += 1

            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

    return threads, pages_read


async def resolve_review_threads(
    owner: str,
    repo: str,
    thread_ids: list[str],
    token: str,
) -> tuple[list[str], list[dict]]:
    """Resolve specific review threads by ID.

    Returns a tuple of (resolved_ids, failures). Each failure is a dict with
    keys: id, error (or status/body, or errors/thread).

    Raises GitHubAPIError on non-success response or GraphQL errors on the fetch.
    Individual resolution failures are captured in the failures list.
    """
    _require_token(token, "resolve_review_threads")
    graphql_url = get_settings().GITHUB_GRAPHQL_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    mutation = (
        "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) "
        "{ thread { id isResolved } } }"
    )

    resolved_ids: list[str] = []
    failures: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        for tid in thread_ids:
            try:
                resp = await client.post(graphql_url, headers=headers, json={
                    "query": mutation,
                    "variables": {"id": tid},
                })
            except Exception as exc:
                failures.append({"id": tid, "error": str(exc)})
                continue

            if not resp.is_success:
                failures.append({
                    "id": tid,
                    "status": resp.status_code,
                    "body": resp.text[:200],
                })
                continue

            try:
                body = resp.json()
            except Exception as exc:
                failures.append({"id": tid, "error": f"non-json: {exc}"})
                continue

            errors = body.get("errors") or []
            thread_data = ((body.get("data") or {}).get("resolveReviewThread") or {}).get("thread") or {}
            if errors or not thread_data.get("isResolved"):
                failures.append({
                    "id": tid,
                    "errors": errors,
                    "thread": thread_data,
                })
                continue

            resolved_ids.append(tid)

    return resolved_ids, failures


async def list_open_issues(
    owner: str,
    repo: str,
    token: str,
    *,
    first: int = 50,
    max_pages: int = 5,
) -> list[dict]:  # type: ignore[type-arg]
    """Every open issue, with the labels that say whether it was already handled.

    173: the performer had no issue-listing call at all, so a role built on
    inbound issues had nothing to read.  Paged in the same shape as
    ``fetch_review_threads``: a bounded walk, because a repository with
    thousands of open issues must not be enumerated forever.

    Pull requests are issues in GitHub's data model but NOT in this one -- the
    ``issues`` connection excludes them, which is what both intake roles want.

    Raises GitHubAPIError on a non-success status or a GraphQL ``errors`` body.
    """
    _require_token(token, "list_open_issues")
    graphql_url = get_settings().GITHUB_GRAPHQL_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    query = """
    query($owner: String!, $repo: String!, $first: Int!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        issues(states: OPEN, first: $first, after: $cursor,
               orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes {
            id number title body url
            labels(first: 20) { nodes { name } }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """

    issues: list[dict] = []  # type: ignore[type-arg]
    cursor: str | None = None
    pages_read = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while pages_read < max_pages:
            resp = await client.post(graphql_url, headers=headers, json={
                "query": query,
                "variables": {"owner": owner, "repo": repo, "first": first, "cursor": cursor},
            })
            if not resp.is_success:
                raise GitHubAPIError(resp.status_code, resp.text[:200])

            payload = resp.json()
            if payload.get("errors"):
                raise GitHubAPIError(200, str(payload["errors"]))

            connection = (
                ((payload.get("data") or {}).get("repository") or {}).get("issues") or {}
            )
            for node in connection.get("nodes") or []:
                if not node:
                    continue
                label_nodes = ((node.get("labels") or {}).get("nodes")) or []
                issues.append({
                    "id": str(node.get("id") or ""),
                    "number": int(node.get("number") or 0),
                    "title": node.get("title") or "",
                    "body": node.get("body") or "",
                    "url": node.get("url") or "",
                    "labels": [str(x.get("name") or "") for x in label_nodes if x],
                })

            pages_read += 1
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

    return issues


async def add_labels(
    owner: str,
    repo: str,
    labelable_id: str,
    label_names: list[str],
    token: str,
) -> None:
    """Apply existing labels to an issue or pull request by node id.

    173: labelling lived only in coordinare.  Labels are resolved by name first
    because ``addLabelsToLabelable`` takes ids; a name with no label raises
    rather than passing silently, since a label that never lands is what makes
    an advocate re-answer the same issue on the next cycle.

    This never CREATES a label: coordinare's bootstrap already ensures the two
    advocate labels exist, and a run inventing labels is a surprise.
    """
    if not label_names:
        return
    _require_token(token, "add_labels")
    graphql_url = get_settings().GITHUB_GRAPHQL_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    label_query = """
    query($owner: String!, $repo: String!, $name: String!) {
      repository(owner: $owner, name: $repo) { label(name: $name) { id } }
    }
    """
    mutation = (
        "mutation($labelableId: ID!, $labelIds: [ID!]!) { "
        "addLabelsToLabelable(input: {labelableId: $labelableId, labelIds: $labelIds}) "
        "{ clientMutationId } }"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        label_ids: list[str] = []
        for name in label_names:
            resp = await client.post(graphql_url, headers=headers, json={
                "query": label_query,
                "variables": {"owner": owner, "repo": repo, "name": name},
            })
            if not resp.is_success:
                raise GitHubAPIError(resp.status_code, resp.text[:200])
            payload = resp.json()
            if payload.get("errors"):
                raise GitHubAPIError(200, str(payload["errors"]))
            label = ((payload.get("data") or {}).get("repository") or {}).get("label") or {}
            label_id = str(label.get("id") or "")
            if not label_id:
                raise GitHubAPIError(404, f"label {name!r} does not exist in {owner}/{repo}")
            label_ids.append(label_id)

        resp = await client.post(graphql_url, headers=headers, json={
            "query": mutation,
            "variables": {"labelableId": labelable_id, "labelIds": label_ids},
        })
        if not resp.is_success:
            raise GitHubAPIError(resp.status_code, resp.text[:200])
        payload = resp.json()
        if payload.get("errors"):
            raise GitHubAPIError(200, str(payload["errors"]))


async def add_item_to_project(project_id: str, content_id: str, token: str) -> str:
    """Add an issue to a project board, returning the new board item id.

    173: this lived only in coordinare, where an unset project id makes it return
    None -- indistinguishable from a successful add that produced no item.  Here
    an empty project id RAISES before any request, because a curator that cannot
    reach the board must report that rather than look like it promoted nothing.

    Setting the item's column is a separate mutation: this call only adds.
    """
    _require_token(token, "add_item_to_project")
    if not project_id.strip():
        raise GitHubAPIError(400, "project_id is required to add an item to a board")
    graphql_url = get_settings().GITHUB_GRAPHQL_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    mutation = (
        "mutation($projectId: ID!, $contentId: ID!) { "
        "addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) "
        "{ item { id } } }"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(graphql_url, headers=headers, json={
            "query": mutation,
            "variables": {"projectId": project_id, "contentId": content_id},
        })
    if not resp.is_success:
        raise GitHubAPIError(resp.status_code, resp.text[:200])
    payload = resp.json()
    if payload.get("errors"):
        raise GitHubAPIError(200, str(payload["errors"]))
    item = ((payload.get("data") or {}).get("addProjectV2ItemById") or {}).get("item") or {}
    item_id = str(item.get("id") or "")
    if not item_id:
        raise GitHubAPIError(200, "addProjectV2ItemById returned no item")
    return item_id


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

    # Fetch threads (best effort: on GitHubAPIError log and return 0)
    try:
        threads, _pages = await fetch_review_threads(owner, repo, pr_number, token, max_pages=1)
    except GitHubAPIError as exc:
        log.warning("resolve_threads.fetch_failed", status=exc.status_code, body=str(exc))
        return 0

    unresolved = [t for t in threads if not t.get("resolved")]
    if not unresolved:
        return 0

    def _first_author(thread: dict) -> str:  # type: ignore[type-arg]
        comments = thread.get("comments") or []
        if comments:
            # fetch_review_threads normalises a null author to "", which is a
            # present key: keep the historical "unknown" for the log field.
            return comments[0].get("author") or "unknown"
        return "unknown"

    # Resolve threads and capture failures with original metadata
    unresolved_ids = [t["id"] for t in unresolved]
    resolved_ids, raw_failures = await resolve_review_threads(owner, repo, unresolved_ids, token)

    # Enrich failures with author and outdated info from original threads
    failed: list[dict] = []
    for failure in raw_failures:
        tid = failure.get("id", "")
        thread = next((t for t in unresolved if t["id"] == tid), {})
        author = _first_author(thread)
        outdated = thread.get("outdated", False)
        # Preserve the failure info and add metadata
        enriched = dict(failure)
        enriched["author"] = author
        enriched["outdated"] = outdated
        failed.append(enriched)

    log.info(
        "resolve_threads.done",
        owner=owner, repo=repo, pr_number=pr_number,
        attempted=len(unresolved), resolved=len(resolved_ids), failed=len(failed),
    )
    if failed:
        log.warning(
            "resolve_threads.partial_failure",
            pr_number=pr_number, failed=failed,
        )
    return len(resolved_ids)


# Contract: IssueCommentService

**Module**: `src/coordinare/services/issue_comment_service.py`

## Interface

```python
async def fetch_new_issue_comments(
    issue_number: int,
    since_id: int | None,
    github_client: GitHubClient,
) -> list[IssueCommentEvent]:
    """Fetch comments on the given issue posted after since_id.

    since_id: highest previously-seen comment ID; None means fetch all.
    Returns comments in ascending created_at order.
    Returns [] if issue_number is None or the issue has no new comments.
    """
```

## Behavior Guarantees

- Returns an empty list (never raises) if the issue does not exist or the API call fails (logs warning).
- Results are ordered by `comment_id` ascending.
- Bot comments (login contains `[bot]`) are included in results; the assessor is responsible for classifying them as `noise`.
- The caller is responsible for tracking `processed_issue_comment_ids`; this function does not filter by processed IDs.

## GitHub REST Endpoint

```
GET /repos/{owner}/{repo}/issues/{issue_number}/comments
    ?per_page=100
    &since={last_seen_created_at}   # derived from since_id via prior comment metadata
```

Pagination: follow `Link: <url>; rel="next"` headers if present. In practice, cards rarely accumulate > 100 comments between cycles.

## Integration Point

Called from `route_issue_comments` LangGraph node each cycle for each active card with a linked issue.

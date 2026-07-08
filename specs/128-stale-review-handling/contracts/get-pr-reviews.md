# Contract: Extended `get_pr_reviews` (GraphQL query + parsed shape)

## Field Registry

| Field | Source | Type | Notes |
|-------|--------|------|-------|
| `id` | `PullRequestReview.id` | str | existing |
| `author_login` | `PullRequestReview.author.login` | str | existing |
| `state` | `PullRequestReview.state` | str | existing (APPROVED/CHANGES_REQUESTED/COMMENTED/…) |
| `body` | `PullRequestReview.body` | str | existing |
| `submitted_at` | `PullRequestReview.submittedAt` | str\|null | existing |
| `comments` | `PullRequestReview.comments.nodes[].body` | list | existing (inline comment bodies) |
| **`commit_oid`** | `PullRequestReview.commit.oid` | str | **NEW** — commit the review was submitted against |
| **`review_threads`** | `pullRequest.reviewThreads.nodes[]` | list | **NEW** — `{id, isResolved, review_id?}` per thread |

## Query changes (`GET_PR_REVIEWS_QUERY` in `services/github.py`)

Add to each `reviews.nodes` entry:
```graphql
commit { oid }
```
Add a sibling selection on the pull request:
```graphql
reviewThreads(first: 100) {
  nodes {
    id
    isResolved
    comments(first: 1) { nodes { pullRequestReview { id } } }   # attribute thread → review
  }
}
```

## Parsed output contract (`get_pr_reviews` return)

Each review dict gains `"commit_oid": str` (default `""` when absent). The method additionally returns (or a sibling accessor returns) `review_threads: list[{"id": str, "is_resolved": bool, "review_id": str|None}]`.

## Contract test (`tests/unit/services/test_github_reviews.py`)

- Given a mocked GraphQL response containing `commit{oid}` and `reviewThreads` with mixed `isResolved`, `get_pr_reviews` MUST expose `commit_oid` per review and the resolved/unresolved threads with their `review_id` attribution.
- Given a response omitting `commit`/`reviewThreads` (older-shape / partial), parsing MUST NOT raise — `commit_oid` defaults `""`, threads default `[]`.
- Pagination note: threads capped at 100/first page — if a PR exceeds that, log the truncation (a silent cap would misjudge "all resolved"); treat unknown remainder as unresolved (safe).

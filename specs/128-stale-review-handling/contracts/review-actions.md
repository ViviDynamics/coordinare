# Contract: Review actions (re-request review, resolve thread)

Two new GitHub GraphQL mutations, both fail-safe (permission/API error → log + surface, never raise into the poll cycle).

## `request_reviews(pr_id, reviewer_logins)` — coordinare (`services/github.py`)

Re-request review from the original reviewer(s) of a stale-addressed change-request.

```graphql
mutation($prId: ID!, $userIds: [ID!]!) {
  requestReviews(input: { pullRequestId: $prId, userIds: $userIds, union: true }) {
    pullRequest { id }
  }
}
```

- `union: true` — adds to any existing requested reviewers (does not clobber).
- Reviewer login → node id resolved via a `user(login:)` lookup (or cached).
- **Contract test**: given a stale-addressed review by human `X`, `monitor_pr` calls `request_reviews(pr, ["X"])` exactly once per situation (dedup); on a mocked permission error it logs + leaves the card parked and does not raise.

## `resolve_review_thread(thread_id)` — performer (`agent/performer/.../github.py`)

Called by the implementer for each inline thread it addressed.

```graphql
mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread { id isResolved }
  }
}
```

- Implementer also posts a short reply on the thread: `addressed in <commit_sha>`.
- **Contract test**: after the implementer addresses a thread's feedback, `resolve_review_thread` is called with that thread id and the thread reports `isResolved: true`; unaddressed threads are not resolved.

## Explicitly NOT added

- `dismissPullRequestReview` — MUST NOT be used (FR-005: never auto-dismiss a human verdict).
- No approval mutation of any kind by coordinare.

## Notification contract (`notify.py` / `EventType.stale_review_surfaced`)

- Fires once per `(pr_id, review_id, head_oid)` (dedup key).
- Payload MUST include: reviewer login, review submitted date, review commit (short), current head (short), PR number, review id, and next action string `"re-review or dismiss review <id> on PR #<n>"`.
- Severity `warning`.

# Research: Closer Workflow

Resolved by reading the code on main `d7971f2`.

## R-a. Everything the closer may decide is already in GitHub

`resolve_pr_review_threads` (agent/performer/src/performer/github.py:381) already queries `reviewThreads(first: 100)` for `id`, `isResolved`, `isOutdated` and the first comment's author, and resolves each unresolved thread with `resolveReviewThread`. The closer needs the same query widened to `path`, `line` and all comments (author login, body, createdAt), and the mutation applied to chosen ids rather than all. Both are extracted as `fetch_review_threads(owner, repo, pr, token, max_pages=5)` and `resolve_review_threads(owner, repo, thread_ids, token)`; `resolve_pr_review_threads` keeps its signature and return (the count) and becomes a thin caller, so the reviewer path is unchanged.

## R-b. The classification rules

`resolved`: `isResolved`. `stale`: not resolved and `isOutdated`, which GitHub sets when the diff hunk the thread anchored to no longer exists, the exact signal the persona calls "clearly addressed by a subsequent commit". `answered`: not resolved, not outdated, and the last comment's author differs from the first comment's author and its `createdAt` is not earlier. `open`: everything else, including a thread whose only later comments are the raiser's own. Only `answered` is ambiguous, so only it reaches the model.

## R-c. Why the quote is the gate

The model's judgement is checked by searching the thread's own comment bodies for the returned quote, whitespace folded, exactly as the spec-169 reviewer checks evidence against the diff. A judgement that cannot be traced to a comment is discarded and the thread stays open. This is the same shape that caught hallucinated findings in 169 and 170.

## R-d. Order of effects

The review is posted before any thread is resolved: a post failure then leaves the PR untouched, and the hold is honest. Resolution happens only on a passing verdict, and a resolution failure turns the verdict into a hold, because a card must never advance carrying a thread the closer believed closed.

## R-e. main.py and coordinare

The closing_review role shares the reviewer's branch (main.py:2090). The workflow branch is keyed on `perf.role == "closing_review"` and a `closing` report key with a known verdict, placed before that shared branch, and maps to `approved` (with `resolve_pr_review_threads` NOT called again, the workflow already resolved) or `changes_requested` with comments naming the open threads, keeping `REVIEWER_MAX_CYCLES`. `env_blocked` carries the hold. Coordinare is unchanged.

## R-f. CI stays out

`_CLOSER_PR_CHECKS_DIRECTIVE` (persona_service.py:52) exists because closers rejecting on pending checks caused a bounce loop; spec 064's rollup gate runs after approval. The workflow therefore never reads check status, which also removes the persona's `status`, `failed_jobs` and `pending_jobs` output fields.

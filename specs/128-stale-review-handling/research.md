# Research: Stale / Addressed Human Review Handling

## Decision 1 — Staleness signal: review commit oid vs PR head oid

**Decision**: A human `CHANGES_REQUESTED` review is **stale** when the commit it was submitted against differs from the PR head, past a configurable threshold (commits-behind and/or hours-behind since submission). GitHub exposes `PullRequestReview.commit.oid`; the PR head is `pullRequest.headRefOid` (already fetched via `check_mergeability`).

**Rationale**: This is the exact signal that distinguishes #111's 7-week-old review (commit dozens behind head) from a review the human just left on the current head. It requires no heuristic content analysis — purely structural. `get_pr_reviews` does **not** currently return the per-review commit; extending the GraphQL query one field is the minimal change.

**Alternatives considered**: (a) timestamp-only (review age) — rejected: an old review on the *current* head is still genuinely outstanding. (b) diffing review body against current code — rejected: fragile, model-dependent. Commit-oid + optional age is deterministic.

## Decision 2 — Addressed signal: resolved threads, or body-only + new commits

**Decision**: A stale change-request is **addressed** when (a) it has inline threads and **all** are `isResolved`, OR (b) it is body-only (no inline threads, like #111) and new commits have landed since the review. Mixed (some unresolved) → **not** addressed → surface as needing attention but do not auto-advance.

**Rationale**: Thread resolution is the reliable per-item "done" signal (and US2 makes the implementer set it). Body-only reviews have no threads to resolve, so commit-progress is the only available signal — acceptable because the human is re-requested anyway (they get the final say).

**Alternatives considered**: treating body-only reviews as never-addressed — rejected: that's the exact #111 trap (would stay silently blocked). Requiring threads always — rejected: many human verdicts are body-only.

## Decision 3 — Re-request review (not auto-dismiss)

**Decision**: For stale-addressed, call GitHub `requestReviews` (GraphQL mutation, `union: true`) to re-request the original reviewer(s), and route the card to IN_REVIEW. Never `dismissPullRequestReview` / never approve.

**Rationale**: Operator directive + the "only humans approve/gate" constitution principle. Re-requesting puts the ball in the human's court and makes the card's state honest (IN_REVIEW, not silently BLOCKED) without coordinare clearing a human verdict.

**Alternatives considered**: auto-dismiss stale verdict (rejected by operator — overrides human gate); leave BLOCKED + notify only (rejected — card doesn't visibly return to the review flow).

## Decision 4 — Implementer resolves inline threads as it fixes (US2)

**Decision**: Add an implementer step that, for each inline review thread it addresses, calls `resolveReviewThread(threadId)` and leaves a short comment naming the fixing commit. Threads it doesn't address stay unresolved.

**Rationale**: Produces the Decision-2 "addressed" signal reliably and speeds the human re-review. Uses the same GraphQL client already in the performer's `github.py`.

**Alternatives considered**: coordinare-side auto-resolve of all threads on new commits — rejected: coordinare can't know a thread's concern was actually fixed; only the implementer that made the fix can attest.

## Decision 5 — Dedup + fail-safe

**Decision**: Dedup key = `(pr_id, gating_review_id, head_oid)` persisted per card; re-request + notification fire once per situation and only re-fire when the head advances or a new gating review appears. All new GitHub mutations are wrapped so a permission/API failure logs + surfaces and leaves the card parked — never crashes the poll cycle (mirrors existing node error handling).

**Rationale**: FR-004 (one notification per situation) + FR-012 (fail safe). Matches the existing `notify.py` dedup-key pattern and the daemon's per-node exception guards.

**Alternatives considered**: notify every cycle (rejected — spam); no persistence (rejected — would re-notify each restart).

## Decision 6 — Threshold configuration

**Decision**: Config-driven threshold with a safe default — default treats a review as stale only when its commit != head AND at least 1 later commit exists (i.e., new work landed). Optional hours-behind guard configurable. Default MUST never auto-advance a review on the current head.

**Rationale**: FR-010. A conservative default preserves US3 (fresh reviews unchanged) out of the box; operators can tune sensitivity.

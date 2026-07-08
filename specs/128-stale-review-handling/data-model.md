# Data Model: Stale / Addressed Human Review Handling

## Review (extend existing `models/review.py`)

Existing fields: `id`, `author_login`, `author_type` (`ReviewerType`: HUMAN / TRUSTED_BOT / UNTRUSTED), `state` (`ReviewState`), `body`, `submitted_at`, `is_actionable`, inline `comments`.

**Added field:**
- `commit_oid: str` — the commit SHA the review was submitted against (from `PullRequestReview.commit.oid`). Empty string if GitHub omits it.

**Validation**: unchanged; `commit_oid` optional (defaults `""`) so v-prior data / partial payloads load.

## ReviewThread (new lightweight model)

Represents an inline review conversation on the PR.
- `id: str` — thread node id (for `resolveReviewThread`).
- `is_resolved: bool`
- `review_id: str | None` — the review this thread belongs to (to attribute threads to the gating review), when derivable.

## StalenessClass (new enum — the evaluator output)

- `FRESH` — review commit is at/within threshold of head, or has unresolved threads on head → existing behavior applies.
- `STALE_ADDRESSED` — review commit behind head past threshold AND (all its threads resolved OR body-only with new commits) → re-request + IN_REVIEW.
- `STALE_UNADDRESSED` — review commit behind head but threads still unresolved → surface as needing attention; do NOT auto-advance (stays parked, notified once).

## Evaluator input/output (pure function in `services/review_staleness.py`)

**Input**: gating human review (`Review`), current PR `head_oid`, `commits_behind` (or the commit list), that review's threads (`list[ReviewThread]`), config threshold.
**Output**: `StalenessClass` + a reason string (for logging/notification).
**Rules**: encode FR-001, FR-002, FR-007; deterministic, no I/O.

## PersistedSession marker (extend `PersistedSession`, schema-version bump)

- `surfaced_stale_reviews: dict[str, str]` — maps `gating_review_id → head_oid` at the moment it was surfaced/re-requested. Used as the dedup key (FR-004): re-request + notify only when the entry is absent or the head has advanced past the recorded oid. Backward-compatible default `{}` (v-prior snapshots load empty).

## Notification (extend `models/notification.py`)

- `EventType.stale_review_surfaced` — new event. Severity `warning`. Payload: `pr`, `review_id`, `reviewer`, `review_date`, `review_commit`, `head_commit`, `next_action`. `dedup_key = f"stale_review:{pr_id}:{review_id}:{head_oid}"`.

## State transition

`BLOCKED (silent)` → **`IN_REVIEW` + re-review requested + one `stale_review_surfaced` notification**, for a `STALE_ADDRESSED` classification. `STALE_UNADDRESSED` → parked + one notification (no state flip, no re-request). `FRESH` → unchanged existing path. When the human approves/dismisses → normal merge path (existing).

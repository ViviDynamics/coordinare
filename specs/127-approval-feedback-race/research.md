# Research: Approval/Feedback Race (127)

## R1 — Where the race lives and why the feedback is dropped permanently

**Decision**: Fix inside `monitor_pr`'s review-evaluation block only (`src/coordinare/graph/nodes/monitor_pr.py:186-253`); no changes to `classify_human_feedback`, `merge_pr`, or the github service.

**Rationale**: Verified control flow (2026-07-03 architectural review + direct read):
- Line 221-222: `approved = True` on any HUMAN `APPROVED` review in the filtered batch.
- Line 223-224: actionable (`COMMENTED`/`CHANGES_REQUESTED` from HUMAN or TRUSTED_BOT) collected into `actionable` and stored to `state["pending_reviews"]` (line 226).
- Line 227 `if approved:` → phase `merging`; line 244 `elif actionable:` → phase `relay_feedback` is *skipped* whenever an approval coexists.
- The drop is permanent, not deferred: once phase is `merging`, `merge_pr` runs; the actionable reviews were never handed to `classify_human_feedback`, so their IDs are never added to `processed_review_ids` — but no node ever looks at them again because the card leaves the monitoring loop.

**Alternatives considered**: fixing at `merge_pr` (add a pre-merge unprocessed-actionable check) — rejected: it duplicates review fetching/filtering, and by then `pending_reviews` may be stale; the decision point that has the full batch in hand is `monitor_pr`.

## R2 — How a deferred approval survives without new state

**Decision**: No persisted flag. Invert precedence (`if actionable: → relay`, `elif approved: → merge`) and rely on `processed_review_ids` semantics: approvals never enter `pending_reviews`, and `classify_human_feedback` only marks the IDs it classified (`classify_human_feedback.py:355-357`), so a deferred approval remains unprocessed and re-surfaces on every later poll.

**Rationale**: Matches spec Key Entities ("implicit state — re-derived from PR review state"). Trace of the deferral round-trip:
1. Cycle N: batch = {approval A, change-request B} → phase `relay_feedback`; B classified, `processed_review_ids += {B}`. A untouched.
2. Cycle N+k (feedback handled without new commits — e.g. COMMENTED question classified as ignorable): batch = {A} (B filtered as processed) → `approved`, nothing actionable → merging. FR-003 satisfied, no re-approval.
3. If handling B bounced the card and the lifecycle re-completed, `lifecycle_completed_at` (cutoff, lines 177-183) now postdates A's `submitted_at` → A is filtered → merge requires fresh approval. This is the spec's documented Edge Case ("stale-approval policy… is repository policy"); confirmed intentional, not a gap.

**Alternatives considered**: persisting a `deferred_approval_id` on the session — rejected: new schema field for a bug fix, and it would *wrongly* merge on a stale approval after code changed (worse than the conservative cutoff behaviour).

## R3 — Per-reviewer supersession source data

**Decision**: Group the filtered batch by `author_login`, keep each author's latest review by `submitted_at`; approval and deferral decisions read only these latest-state reviews. Ties/unparseable timestamps → the actionable state governs.

**Rationale**: `github.get_pr_reviews` already returns `id`, `author_login`, `state`, `submitted_at` (ISO-8601 from GraphQL `submittedAt`) — verified at `src/coordinare/services/github.py:1443-1474`; GraphQL `reviews(last: 50)` gives chronological data for well beyond any realistic same-batch window. Timestamp parsing reuses the `datetime.fromisoformat(... .replace("Z", "+00:00"))` idiom already in the module (line 183, 213). GitHub's own review model treats a reviewer's later review as superseding their earlier one, so this mirrors platform semantics (spec Assumptions).

**Alternatives considered**: using GraphQL `reviewDecision` (repo-computed aggregate, already fetched) — rejected as the *primary* signal: it reflects branch-protection configuration (returns null on repos without required reviews) and collapses the batch, so coordinare couldn't tell *which* reviews are actionable or unprocessed. It remains a possible future cross-check, out of scope here.

## R4 — What must not change (regression surface)

**Decision**: Keep the 090 baseline-prevention gate call inside the approval branch exactly as-is; keep `state["pending_reviews"] = actionable` assignment; keep the `else: monitoring_pr` fallthrough; keep cutoff and processed-ID filtering untouched upstream of the new logic.

**Rationale**: FR-006/SC-004 demand byte-identical behaviour for approval-only and actionable-only batches. Existing tests (`tests/unit/` monitor_pr coverage) plus two new explicit regression tests pin this.

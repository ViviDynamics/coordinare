# Contract: monitor_pr review routing (127)

Behavioural contract for the review-evaluation block of `monitor_pr`. Consumed by unit tests in `tests/unit/graph/nodes/test_monitor_pr_approval_race.py`.

## Inputs

- `reviews`: list of review dicts from `github.get_pr_reviews` (fields: `id`, `author_login`, `state`, `body`, `submitted_at`, `comments`)
- `state["human_reviewers"]`, `state["trusted_bot_reviewers"]`: reviewer classification lists
- `state["lifecycle_completed_at"]`: cutoff (unchanged semantics)
- `state["processed_review_ids"]`: already-classified review IDs (unchanged semantics)

## Supersession rule (applied after cutoff/processed filtering)

For each `author_login`, exactly one review is *effective*: the one with the greatest parseable `submitted_at`. Tie or unparseable timestamp on either side → an actionable-state review beats `APPROVED`. Reviews with empty `author_login` are each effective (no grouping).

## Decision table

| # | Effective batch contents | Required outcome |
|---|---|---|
| 1 | ≥1 actionable (HUMAN or TRUSTED_BOT, `CHANGES_REQUESTED`/`COMMENTED`), regardless of approvals | `state["pending_reviews"]` = the effective actionable reviews; `phase = "relay_feedback"`; NO merge. If a HUMAN `APPROVED` also present → emit `monitor_pr.merge_deferred` with `card_id`, `approval_review_id`, `actionable_review_ids`. |
| 2 | ≥1 HUMAN `APPROVED`, zero actionable | 090 baseline-prevention gate evaluated exactly as today; if it does not stop, `phase = "merging"`. |
| 3 | zero approvals, zero actionable | `phase = "monitoring_pr"`. |

## Per-reviewer supersession cases

| # | Same reviewer, same batch | Required outcome |
|---|---|---|
| S1 | `CHANGES_REQUESTED` @ t1, `APPROVED` @ t2 > t1 | approval effective → row 2 (merge path). |
| S2 | `APPROVED` @ t1, `CHANGES_REQUESTED` @ t2 > t1 | change request effective → row 1 (relay; no deferral event since no effective approval). |
| S3 | `APPROVED` @ t, `CHANGES_REQUESTED` @ t (tie) | change request effective (conservative) → row 1. |
| S4 | reviewer A `APPROVED`, reviewer B `CHANGES_REQUESTED` (any order) | both effective → row 1 with deferral event (cross-reviewer, no supersession). |

## Invariants (regression guards)

- I1: Approval-only batches take the identical code path as today including the 090 base-gate call (byte-identical routing, FR-006).
- I2: Actionable-only batches take the identical code path as today (FR-006).
- I3: A deferred approval's review ID MUST NOT be added to `pending_reviews` nor (transitively) to `processed_review_ids`.
- I4: Every review dict in `pending_reviews` carries `author_type` (existing enrichment, unchanged).
- I5: No additional GitHub API calls are made by the new logic.

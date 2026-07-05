# Data Model: Approval/Feedback Race (127)

No persisted-state change. **No snapshot schema bump.** All entities are transient, scoped to one `monitor_pr` evaluation.

## Evaluation batch (transient)

The list of reviews that survive the two existing filters (both unchanged by this feature):

| Filter | Source | Semantics |
|---|---|---|
| cutoff | `state["lifecycle_completed_at"]` | reviews submitted at/before the last lifecycle completion are ignored |
| processed | `state["processed_review_ids"]` (persisted, existing) | reviews already handed to classification are ignored |

Each review dict (shape produced by `github.get_pr_reviews`, unchanged):
`{id: str, author_login: str, state: str, body: str, submitted_at: str|None, comments: list, author_type: str}`.

## Effective reviews (new, transient)

Per-author latest-state projection of the evaluation batch:

- **Key**: `author_login` (empty login → never superseded, treated as distinct conservative entries).
- **Value**: the author's review with the greatest parseable `submitted_at`; when timestamps tie or fail to parse, the review with an actionable state (`CHANGES_REQUESTED`/`COMMENTED`) wins over `APPROVED` (conservative rule, spec Assumptions).
- **Derived signals** (replacing today's raw-batch derivation):
  - `approved` := any effective review with `author_type == HUMAN` and `state == APPROVED`
  - `actionable` := effective reviews with `author_type ∈ {HUMAN, TRUSTED_BOT}` and `state ∈ {COMMENTED, CHANGES_REQUESTED}`

## Routing decision (changed)

| approved | actionable | Phase (today) | Phase (127) |
|---|---|---|---|
| yes | none | merging (after 090 base gate) | merging (after 090 base gate) — unchanged |
| yes | ≥1 | **merging — feedback dropped** | **relay_feedback + `monitor_pr.merge_deferred` event** |
| no | ≥1 | relay_feedback | relay_feedback — unchanged |
| no | none | monitoring_pr | monitoring_pr — unchanged |

## Deferred approval (implicit)

Not stored. An approval that coexisted with actionable reviews is simply *not consumed*: its ID never enters `pending_reviews`, hence never enters `processed_review_ids`, hence it re-appears in later evaluation batches until it either governs (row 1) or is filtered by a moved cutoff after a code-changing bounce (repository branch-protection then owns re-approval policy).

## State transitions

None added. `phase` transitions reuse the existing values (`merging`, `relay_feedback`, `monitoring_pr`); `pending_reviews` keeps its existing shape (now always the effective actionable set).

# Process review feedback received during work

Issue #548

## Scope
In: classify unconsumed actionable reviews after lifecycle completion; retain approval freshness and per-author supersession; durably deduplicate accepted override commands without dropping other reviews.
Out: ordinary PR conversation comments (#550), pause behavior (#547/#552), and closed PR handling (#551).

## Assumptions
- Completion time cannot prove that a submitted review was addressed.
- Accepted override intent and its consumed review ID must survive restart together.
- Old approvals may supersede old requests but cannot authorize a new lifecycle merge.

## Tasks
- [x] 1. Reproduce body and inline reviews dropped at completion, including snapshot restore.
- [x] 2. Deduplicate accepted commands and persist pending override intent without consuming unrelated feedback.
- [x] 3. Keep approval freshness, per-author supersession, and timezone compatibility under regression tests.
- [ ] 4. Advance snapshot schema after #549 lands, verify full preflight and adversarial/Copilot review, then merge.

# Preserve completed performer handoffs and PR identity

Issues #557 and #559

## Scope

In: pending final PR-check handoff without a live performer, durable per-card PR artefacts across JSON snapshots and board hydration, closed/reopened same-PR monitoring, and actual daemon-cycle regressions.
Out: changing reviewer-stage configuration, weakening in-flight guards, changing capacity or approvals, and unrelated sample features.

## Assumptions

- A successful worker can be gone while required checks remain pending. That is a gate-only wait, not a stale worker needing replacement.
- Board metadata cannot recreate a closed PR; retain only the known PR artefact fields per card, not arbitrary performer output.
- Existing snapshots load with safe defaults; a new schema documents new durable handoff state.

## Tasks

- [x] Reproduce final-stage success with pending checks through the actual daemon graph and next poll; assert no new dispatch, same PR identity, gate proceeds when green.
- [x] Reproduce unfocused JSON restore and Backlog close/reopen using actual board/monitor nodes; assert closed-specific Blocked and no implementation churn after explicit Todo.
- [x] Implement the minimal handoff/persistence changes, retaining live-writer exclusion, feedback, and approval boundaries.
- [ ] Run focused regressions and full preflight; adversarial and Copilot review; current-head required CI; squash merge and verify both closures.
- [ ] Verify released image/source rollout and re-exercise the two live failures in the isolated sample.

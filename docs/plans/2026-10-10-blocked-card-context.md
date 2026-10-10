# Keep blocked-card context and replies isolated

Issues #24 and #26

## Scope
In: hydrate clean context when adopting a new blocked card, focus that card atomically, route replies to the already focused blocked card rather than a sibling, and resume eligible Todo sessions whose phase remains blocked after status reconciliation.
Out: migration/repair of already contaminated snapshots, lifecycle selection, board approval gates and infrastructure settings.

## Assumptions
- Flat card-scoped fields may remain after a previous card retires; a new identity cannot inherit them.
- Existing same-card state may contain fresh questions not yet serialized and must remain intact.
- Human replies belong to the issue whose card is currently being handled.

## Tasks
- [x] 1. Reproduce foreign-context inheritance and wrong-issue reply routing through actual handler/finalizer/session serialization.
- [x] 2. Bind newly adopted card context and focus together; preserve same-card updates and other sessions.
- [x] 3. Route focused replies correctly with multiple blocked siblings; verify no-reply/legacy/manual-hold neighbors.
- [x] 4. Resume explicit eligible Todo from blocked phase without clearing owned holds or duplicating a live worker.
- [ ] 5. Run full preflight, exact-head CI, fresh configured review; deploy and replay original interactions without editing snapshots.

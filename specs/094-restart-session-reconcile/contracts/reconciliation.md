# Contract: Restart-Time Session Reconciliation

This feature has no external API surface. Its contracts are (1) the internal
reconciliation behavior over restored sessions and (2) the observability event
shape. Both are verified by unit tests.

## Field Registry

This feature adds no fields to any cross-boundary dispatch payload, persisted
schema, or performer contract. (Registry intentionally empty — the
`/speckit.analyze` contract check is a no-op for 094.)

## Behavioral contract: `_reconcile_with_board` (extended)

**Trigger**: once, in the startup recovery path (existing call site daemon.py:2242), after sessions are restored from the snapshot and before the first board cycle.

**Inputs**: the loaded `WorkflowSnapshot` (top-level focus + `active_sessions`); the live board via `github_service.poll_board()`.

**Guarantees**:
1. For each restored session in `active_sessions`, look up its card's current board column. (C1, FR-001)
2. If the board-inferred phase differs from the persisted phase and the session is not a preserved in-flight session, correct the session's phase to the board-inferred value (board wins). (C2, FR-002/FR-003)
3. A session legitimately `monitoring_pr` (with PR reference) or `monitoring_performer` (with live session) whose board column is consistent is left fully intact — PR/performer context preserved. (C3, FR-004)
4. A card whose board column is genuinely `BLOCKED` keeps `phase=blocked` and is not made dispatch-eligible. (C4, FR-005)
5. A session whose card is DONE or absent from a **successful** board read is retired; the top-level focus is cleared if it pointed at a retired/corrected session. (C5, FR-010)
6. On a **failed** board read (exception), no session is modified or deleted; startup proceeds. (C6, FR-009)
7. Each correction emits exactly one `restart_reconcile.session_corrected` event with ids/columns/phases only — no secret values. (C7, FR-006/FR-007)
8. Re-running on an already-consistent restored set produces zero corrections. (C8, FR-008)

## Test contract (unit, deterministic; board poll mocked)

| Test | Asserts | Maps |
|---|---|---|
| `test_blocked_session_for_todo_card_becomes_dispatch_eligible` | #158 repro: session restored blocked/idle, board=TODO → card eligible for dispatch after reconcile | C1,C2 / US1 / SC-001,SC-002 |
| `test_truly_blocked_card_stays_blocked` | board=BLOCKED → phase stays blocked, not dispatched | C4 / US1 / SC-005 |
| `test_monitoring_pr_session_preserved` | `monitoring_pr` + PR ref retained unchanged | C3 / US2 / SC-003 |
| `test_monitoring_performer_session_preserved` | live-performer session + stage retained, not demoted | C3 / US2 / SC-003 |
| `test_done_or_absent_card_session_retired` | DONE/absent on successful read → session retired, focus cleared | C5 / edge / FR-010 |
| `test_board_poll_failure_preserves_all_sessions` | poll raises → no session modified/deleted, startup continues | C6 / edge / FR-009 |
| `test_correction_emits_secretfree_event` | one `session_corrected` event; fields are ids/columns/phases only | C7 / US3 / SC-004 |
| `test_reconcile_is_convergent_noop_on_consistent_state` | second reconcile on corrected state → zero corrections | C8 / edge / SC-006 |

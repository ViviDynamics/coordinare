# Quickstart: Validate Block-Notification Dedup on Rehydration

## Prereqs

- coordinare checked out at branch `069-blocked-notification-rehydration`
- `.venv/bin/pytest` available
- No live Slack / GitHub credentials needed — all checks are unit-level

## 1. Run the targeted unit suite

```bash
.venv/bin/pytest \
  tests/unit/graph/nodes/test_notify.py \
  tests/unit/graph/nodes/test_handle_blocked.py \
  tests/unit/test_state_store.py \
  -v
```

Expected: all tests pass, including the new cases:

- `test_notify_suppresses_card_blocked_when_open_questions_empty` *(US3 / FR-003)*
- `test_notify_suppresses_card_blocked_when_active_session_dispatching` *(US2 / FR-005)*
- `test_notify_dedup_key_includes_open_questions_hash` *(FR-006)*
- `test_notify_suppresses_on_first_tick_after_restart_when_watermark_present` *(FR-004)*
- `test_handle_blocked_does_not_repost_when_session_watermark_within_window` *(US1 / FR-001/FR-002)*
- `test_persisted_session_roundtrips_last_blocked_notified_at`
- `test_v1_snapshot_defaults_last_blocked_notified_at_to_none`

## 2. Reproduce the 2026-05-22 12:02 incident in a unit-level harness

The replay lives in `tests/unit/graph/nodes/test_notify.py` as
`test_replay_card70_restart_does_not_emit_card_blocked`. It:

1. Builds a `CoordinareState` matching the post-rehydration snapshot for
   card #70 (top-level `phase=blocked`, `open_questions=[]` after the drop,
   session-level `last_blocked_notified_at = 2026-05-21T21:32:04Z`).
2. Inserts a fresh `active_sessions[card_id]` entry with
   `phase="monitoring_performer"`.
3. Invokes `notify(state)` once.
4. Asserts `notification_service.dispatch` was called exactly 0 times with
   `EventType.card_blocked`, and at most once total (the
   `card_dispatched` event, if the test fixture also drives that node).

## 3. Manual smoke (optional)

If you want to confirm against a real coordinare instance after merging:

1. `set -a && source .env && set +a`
2. Force a snapshot containing `phase=blocked` for a sandbox card with a
   recent `last_blocked_notified_at` (within 24h).
3. Restart coordinare.
4. Observe: Slack channel receives no `🚫 blocked` post for that card; the
   GitHub issue receives no new reminder comment.

## Pass criteria (Definition of Done for this feature)

- All Phase 1 unit tests pass.
- `.venv/bin/ruff check src/coordinare/graph/nodes/notify.py
  src/coordinare/graph/nodes/handle_blocked.py src/coordinare/state_store.py
  src/coordinare/daemon.py` returns zero warnings.
- No `NEEDS CLARIFICATION` markers remain in any artifact under
  `specs/069-blocked-notification-rehydration/`.
- Spec Success/Acceptance scenarios (User Stories 1/2/3) are each tied to
  at least one passing test.

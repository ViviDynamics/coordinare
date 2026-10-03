# Session pinned to a Done card holds the pickup slot across restarts

Issue #516

## Scope

In: `reconcile_board_state` treats a board status of DONE as terminal for the
active session. When the board snapshot places the active card in DONE, the
per-cycle reconciliation retires the session (via `_retire_active_session`,
trigger `board_card_done_reconciled`), clears the dispatch mirror keys, sets
phase to idle, and returns a new action `done_session_retired`. This runs
regardless of local status and regardless of phase, so it catches the
phase-idle zombie that the monitor's own Done path (`monitor/board.py`) skips
over (`PHASE_TO_EXPECTED_COLUMN` has no `idle` entry) and recovers within one
cycle after a restart.

Out:
- Monitor `_phase_board_reconcile` gate changes. The monitor path works for
  its normal phases; widening the phase map to `idle` changes behaviour for
  every monitor invocation, not just the wedge shape. The per-cycle pass
  already covers the gap.
- Startup reconciliation changes. The per-cycle pass runs at the end of the
  first cycle after boot, so the wedge clears within one poll interval even
  when the persisted session is skipped as persistent.
- Wedge-invariant extension. With Done handled terminally every cycle, the
  "session exists but no progress possible" shape no longer accumulates, so
  the invariant's precondition stays as specified.

## Assumptions

- Board DONE is authoritative: a card in DONE has no remaining work for
  coordinare, whatever the local pin thinks.
- `_retire_active_session` is the single write-site for session retirement
  (066 FR-010) and is idempotent, so calling it on every Done sighting is
  safe.
- Local=DONE + board=DONE must ALSO retire (a restart between "status write"
  and "session retire" otherwise re-wedges), so the Done check precedes the
  agreed-status short-circuit.

## Tasks

- [x] 1. Failing tests: board=DONE + live session (local IN_PROGRESS) retires
      the session and returns `done_session_retired`; board=DONE +
      local=DONE + live session also retires (no agreed short-circuit);
      backwards-move, forward-to-IN_REVIEW and agreed IN_PROGRESS cases
      unchanged. Watch them fail for the right reason.
- [x] 2. Implement the Done-terminal branch in `reconcile_board_state` (+
      docstring). Watch tests pass.
- [x] 3. Replace `test_card_in_done_column_with_local_in_progress_deferred`
      with the new Done-terminal expectations (task 1 covers; delete/adjust
      the stale test so the suite tells one story).
- [x] 4. Full unit suite for regressions; ruff; mypy.

## Round 2 (Copilot review findings, 2026-10-03)

- [x] 5. High: post-cycle-only reconcile left the retired session in
      `sym_state.active_sessions`, so `_update_symphony_state`'s mirror
      re-seeded the zombie every cycle. Fix: `_reconcile_board_state_with_release`
      in daemon.py runs inside `_conduct_single_symphony` BEFORE the mirror;
      `_post_cycle_invariants` reuses the same helper. Regression test:
      cross-cycle no-resurrect (`test_516_done_card_reconcile_release.py`).
- [x] 6. Medium: DONE retirement dropped the session record without
      best-effort resource teardown. Fix: `reconcile_board_state` returns
      `retired_session` (captured before `_retire_active_session`; the
      branch also syncs an unset `active_card_id` so the keyed removal
      fires), and the daemon awaits `_release_session_resources` on it.
- [x] 7. Verification: preflight pass (full unit+integration 508 s), CI green
      at b6da83a, quality guard clean after dropping the coverage pragma the
      guard flags on added diff lines (the except path is now test-covered).

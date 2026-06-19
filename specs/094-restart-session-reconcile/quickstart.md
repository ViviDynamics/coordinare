# Quickstart: Restart-Time Board Reconciliation

## What this delivers

After a daemon restart, every restored per-card session is reconciled against the live board — not just the single top-level focus card. A card the board has moved off BLOCKED (e.g. to TODO) while the daemon was down no longer stays wedged in a stale `blocked`/`idle` session holding a work slot; it becomes dispatch-eligible on the next cycle, automatically. In-flight PR/performer sessions are preserved. The old manual `coordinare.state.json` session-clear is no longer needed.

## Reproduce the bug it fixes (US1 / #158)

1. Have a card on the board in TODO whose persisted session (in `coordinare.state.json` `active_sessions`) carries `phase: "blocked"` (or `idle`).
2. Restart the daemon.
3. **Before this feature**: the card is skipped every cycle (`session_skipped reason=blocked_column` / stuck `phase=idle`), holds a slot, and the "stuck in blocked for N min" alarm fires after the threshold.
4. **After this feature**: at startup the session's phase is reconciled to the board (TODO → eligible); within one board cycle the card is picked up for dispatch, and a `restart_reconcile.session_corrected` log line records `card_id`, `prior_phase`, `board_column`, `corrected_phase`.

## Verify each user story

- **US1 — re-opened card resumes**: snapshot session `blocked`/board `TODO` → card dispatch-eligible after restart; board `BLOCKED` → stays blocked (no false un-block).
- **US2 — in-flight preserved**: snapshot with a `monitoring_pr` session (PR ref) and a `monitoring_performer` session (live session id) → both retain context across restart; no stage demotion.
- **US3 — diagnosable**: each correction emits one `restart_reconcile.session_corrected` event; inspect it — only ids/columns/phases, never secret values.

## Success-criteria checks

- **SC-001/SC-002**: the wedged-card scenario resolves within one board cycle, no manual state-file edit.
- **SC-003**: in-flight sessions retain PR/performer context (no loss, no orphan).
- **SC-004**: every correction is attributable from logs alone.
- **SC-005**: a genuinely-blocked card is never auto-dispatched.
- **SC-006**: restart twice → second restart emits zero corrections (convergent).

## Run the tests

```bash
.venv/bin/pytest tests/unit/test_daemon_restart_reconcile.py -v
.venv/bin/ruff check src/coordinare/daemon.py tests/unit/test_daemon_restart_reconcile.py
```

## Edge cases to confirm

- Board poll fails at restart → no session deleted/modified, daemon still starts.
- Card DONE / absent from a successful board read → session retired, top-level focus cleared if it pointed there.
- Card advanced past persisted stage (board IN_REVIEW, snapshot implementing) → moved forward, not replayed.

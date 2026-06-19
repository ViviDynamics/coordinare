# Phase 0 Research: Restart-Time Board Reconciliation

## R1. Where restored sessions diverge from the board

**Decision**: Extend `Daemon._reconcile_with_board` (daemon.py:805) to iterate `snapshot.active_sessions`, not just the top-level `active_card_id`.

**Findings**:
- On restart, `_reconcile_with_board` polls the board once and reconciles **only `snapshot.active_card_id`** — it finds that one card's column, infers a phase via `_infer_phase_from_board_column`, and updates `self._state["phase"]`. It retires the focus session if the card is gone or DONE (`board_contradicts_snapshot` → `_retire_active_session`).
- The per-card restore loop (daemon.py:667-702) copies each persisted session's `phase` **verbatim** and seeds `current_card` either from the top-level snapshot (for the active card) or as a stub `{"id": card_id}` (for the rest), expecting `check_board` to refresh it from the live board.
- In multi-card mode (`max_concurrent_cards > 1`; the website symphony uses 3), the authoritative per-card state lives in `active_sessions`. Those sessions' phases are never reconciled against the board, so a session restored `BLOCKED`/`idle` for a card now in TODO stays stale.

**Rationale**: The existing method already encapsulates "poll board → find column → infer phase → correct or retire" for one card. The minimal, consistent fix is to apply that same logic per restored session. Reuses `poll_board`, `_infer_phase_from_board_column`, `_retire_active_session`.

**Alternatives considered**:
- *Reconcile lazily in `check_board`/`_compute_eligibility` each cycle*: rejected — eligibility already reads the live board for `blocked_column`, yet the wedge persisted; the stale **phase** is the residue, and per-cycle phase rewriting risks fighting the live phase machine (thrash). Restart-time, run-once reconciliation (FR-008) is the bounded, convergent place.
- *Wipe all sessions and re-adopt fresh from the board*: rejected — violates US2/FR-004 (loses `monitoring_pr` PR state and orphans live performers). The manual #158 fix could wipe one safe session by hand; a blanket wipe is unsafe.

## R2. What exactly to correct — phase, current_card column, or both

**Decision**: Treat as a Phase-0-verified-by-test question; the reproduction test drives it. Working hypothesis: correct the restored session's **phase** (board-inferred) AND ensure its `current_card` carries the live board column, so both the phase machine and `_compute_eligibility` see the post-move state.

**Findings**:
- `_compute_eligibility` (daemon.py:327) blocks a session when its `current_card` content-id is in the live `board_snapshot["BLOCKED"]`. So eligibility already tracks the live board — meaning a TODO card should be *eligible*.
- `_infer_phase_from_board_column` maps TODO/BACKLOG → `idle`. `idle` is also the healthy pre-dispatch resting phase, so "phase=idle" alone is not the wedge — the wedge is an `idle`/`blocked` session that is *also* not being re-picked for dispatch. The reproduction test (T-research) must pin down whether the stale field that suppresses re-pickup is `phase`, the seeded `current_card` column, or the interaction with the top-level focus.

**Rationale**: Encoding the exact corrected field set as a falsifiable test (snapshot in → expect dispatch-eligible out) prevents guessing and satisfies Constitution II/V. The fix is whatever makes that test pass with the smallest correction.

**Alternatives considered**: Specifying the field set up front without a reproduction — rejected (guessing; the live mechanism has subtle phase/eligibility interplay).

## R3. Safe degradation when the board can't be read

**Decision**: Preserve the existing failure semantics and extend them per session: on `poll_board` exception, log `board_reconciliation_skipped` and leave all restored sessions untouched; for an individual card absent from the board snapshot, retire that session (existing `board_contradicts_snapshot` path) rather than guessing.

**Findings**: `_reconcile_with_board` already wraps the poll in try/except and no-ops on failure; it already retires the focus session when its card is absent or DONE. Per-session reconciliation inherits these behaviors.

**Rationale**: FR-009 — never delete a session because of a *failed lookup* (transient outage), but DO retire a session whose card is *confirmed gone* from a *successful* read. The distinction is exactly the existing try/except vs. `found_column is None` split.

**Alternatives considered**: Blocking startup until the board is reachable — rejected (FR-009: must not block startup).

## R4. Observability shape (secret-free, FR-006/FR-007)

**Decision**: Emit one structured `structlog` event per corrected session — e.g. `restart_reconcile.session_corrected` — carrying `card_id`, `prior_phase`, `prior_column`, `board_column`, `corrected_phase`, `symphony`. Mirror the existing `board_reconciliation_advanced` / `board_contradicts_snapshot` event style.

**Findings**: Existing reconciliation already logs `board_reconciliation_advanced/confirmed/skipped` and `board_contradicts_snapshot` with ids/columns/phases only — no secret values. The new per-session event follows the same convention.

**Rationale**: SC-004 — every correction attributable from logs alone. FR-007 — ids/columns/phases/paths only.

**Alternatives considered**: A single summary event for all corrections — rejected (loses per-card attribution needed for SC-004).

## R5. Convergence / no thrash (FR-008, SC-006)

**Decision**: Reconciliation runs once in the startup recovery path (the existing single call site, daemon.py:2242), not in the cycle loop. A session whose persisted phase already matches the board-inferred phase is logged `confirmed` and left unchanged → a second restart produces zero corrections.

**Findings**: The method is already called once at startup, not per cycle. Per-session reconciliation keeps that property.

**Rationale**: Idempotent by construction; SC-006 (no corrections on second restart) follows directly.

## Open items carried to implementation

- **R2** is resolved by writing the reproduction test FIRST (TDD per Constitution II): construct a snapshot reproducing the #158 shape (session `phase=blocked`/`idle`, card on board=TODO) and assert the card becomes dispatch-eligible after reconciliation. The minimal correction that turns that test green defines the corrected field set.

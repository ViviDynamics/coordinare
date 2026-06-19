# Phase 1 Data Model: Restart-Time Board Reconciliation

No persisted-schema changes. This feature reads existing persisted entities and the live board, and corrects in-memory restored state before the first cycle. The next normal persist captures the corrected state. The entities below are existing types (reused) plus one transient observability record.

## Existing entities (reused, unchanged)

### WorkflowSnapshot (`state_store.py:253`)
The on-disk snapshot loaded at restart.
- `active_card_id: str | None`, `active_card_column: str | None`, `phase: WorkflowPhase`, `performer_stage: str | None` — the top-level focus (already reconciled today).
- `active_sessions: dict[str, PersistedSession]` — per-card sessions. **The new reconciliation target.**
- No fields added or removed.

### PersistedSession (`state_store.py:88`)
One restored session per card. Reconciliation reads/corrects:
- `phase: WorkflowPhase` — the field corrected when it diverges from the board-inferred phase.
- In-flight markers that gate preservation (must NOT be reset, FR-004): a `monitoring_pr` phase with a PR reference, or a `monitoring_performer` phase with a live session/performer linkage.
- All other fields (counters, lifecycle bookkeeping, processed-review ids, etc.) are untouched.

### Board Status (live, via `github_service.poll_board()`)
`board["snapshot"]` is a `dict[column_name, list[card_content_id]]`. Reconciliation looks up each session's card content-id to find its current column. **Authoritative source of truth.**

## Phase inference (existing helper, reused)

`_infer_phase_from_board_column(column) -> WorkflowPhase` (daemon.py:795):
| Board column | Inferred phase |
|---|---|
| `IN_PROGRESS` / "in progress" | `monitoring_agent` |
| `IN_REVIEW` / "in review" | `monitoring_pr` |
| `BLOCKED` | `blocked` |
| anything else (TODO, BACKLOG, …) | `idle` |

## Reconciliation outcomes (state transition per restored session)

| Live board column for the card | Persisted phase | Action |
|---|---|---|
| Found, ≠ DONE, board-inferred phase **differs** from persisted | any **non-in-flight** | Correct phase → board-inferred; emit `session_corrected`. |
| Found, ≠ DONE, board-inferred phase **matches** persisted | any | No change; (optional `confirmed` log). |
| Found, card legitimately in-flight (`monitoring_pr`/`monitoring_performer`) and board column consistent | in-flight | **Preserve** PR/performer context unchanged (FR-004). |
| Found = DONE, **or** card absent from a successful board read | any | Retire the session (`_retire_active_session`); if it was the top-level focus, clear the focus (FR-010). |
| Board read failed (exception) | any | **No change to any session** (FR-009); log `board_reconciliation_skipped`. |

After the per-session pass, the top-level focus (`active_card_id` and derived `phase`) is reconciled to remain consistent with the corrected/retired session set (FR-010).

## New transient entity (not persisted)

### Reconciliation Event (`restart_reconcile.session_corrected`)
A structlog record emitted per corrected session. Fields (identifiers/columns/phases only — FR-007):
- `card_id` — the card/session identifier.
- `prior_phase`, `prior_column` — the stale persisted values.
- `board_column`, `corrected_phase` — the live/board-derived values applied.
- `symphony` — the owning symphony name.

Contains **no** secret values (no tokens, no env values, no PR bodies).

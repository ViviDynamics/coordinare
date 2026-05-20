# Implementation Plan: Unify Card Pickup Paths

**Branch**: `066-unify-card-pickup` | **Date**: 2026-05-20 | **Spec**: [./spec.md](./spec.md)
**Input**: Feature specification from `/specs/066-unify-card-pickup/spec.md`

## Summary

Collapse the divergent single-card and multi-card pickup branches in
`src/coordinare/graph/nodes/check_board.py` into a single
session-hydrating path. `_invoke_multi_session` becomes the sole graph
entry point; `max_concurrent_cards=1` operates by limiting slots, not
by selecting a different code path. Top-level `state.current_card`
is demoted to a single-site derived mirror of
`active_sessions[active_card_id]` (FR-010) so SSE/API back-compat is
preserved without independent mutation sites. The IN_REVIEW re-adopt
path is folded into the same unified pickup logic as IN_PROGRESS
(FR-009). v1 (pre-Fix 7) snapshots rehydrate transparently (FR-005).

The work is a structured refactor: no schema change, no client-visible
API change, no graph topology change. Behaviour change is restricted
to "single-card mode now exercises the multi-card code path with N=1."
Risk is concentrated in the read-site migration for the `current_card`
deprecation; every direct read becomes a read of
`active_sessions[active_card_id]`.

## Technical Context

**Language/Version**: Python 3.11 (coordinare)
**Primary Dependencies**: LangGraph (existing), pydantic v2 (existing), structlog (existing)
**Storage**: Existing JSON snapshot at `state_store.py`; schema unchanged. v1 snapshots must round-trip through unified code.
**Testing**: pytest via `.venv/bin/pytest`; coverage budget honoured (no regression). `tests/integration/test_065_*.py` and `tests/unit/graph/nodes/test_check_board*.py` are the primary regression surface.
**Target Platform**: Daemon (Linux/macOS, single process) — no platform-specific code touched.
**Project Type**: Single project (`src/coordinare/` + `tests/`).
**Performance Goals**: Board-scan latency at `max_concurrent_cards=1` within +50ms of pre-066 baseline (SC-002). First-token latency unchanged (refactor only).
**Constraints**: SSE/API payload schema unchanged (FR-006). v1 snapshot back-compat required (FR-005). No new persistent state.
**Scale/Scope**: ~6 source files modified, ~10 test files updated, ~600–900 LOC delta (estimate; refactor is read-site migration heavy).

## Constitution Check

| Principle | Status | Notes |
|---|---|---|
| I. Code Quality First | ✅ Pass | Net reduction in conditional branching; one pickup path replaces two. Type annotations preserved on all touched signatures. |
| II. Testing Discipline | ✅ Pass | All FRs have unit-test mapping (FR-002 cites suites by path). Coverage MUST NOT regress (Phase 3 gate). |
| III. UX Consistency | ✅ Pass | SSE/API schema unchanged (FR-006). Dashboard reads from `active_sessions` but the top-level mirror keeps wire format stable. |
| IV. Performance by Design | ✅ Pass | SC-002 sets the budget (+50ms board scan); verification step in Phase 3. |
| V. Clarity Before Action | ✅ Pass | All three original open questions resolved as FR-009/010/011 before plan started (see spec's Scope Decision Log). |

No violations. Complexity Tracking section is therefore empty.

## Project Structure

### Documentation (this feature)

```text
specs/066-unify-card-pickup/
├── spec.md              # Feature spec (already authored; widened to FR-009/010/011)
├── plan.md              # This file
├── research.md          # Phase 0 output — invariants & migration strategy
├── data-model.md        # Phase 1 output — CoordinareState shape & derivation contract
├── quickstart.md        # Phase 1 output — operator-facing rollout notes
├── contracts/
│   └── current_card-derivation.md  # Internal contract: how the mirror is produced
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/
├── graph/
│   ├── nodes/
│   │   ├── check_board.py          # PRIMARY — collapse two pickup branches into one
│   │   ├── dispatch_performer.py   # READ MIGRATION — current_card → active_sessions[active_card_id]
│   │   ├── monitor_performer.py    # READ MIGRATION
│   │   └── monitor_pr.py           # READ MIGRATION
│   └── state.py                    # Document current_card as derived; add active_card_id helper
├── daemon.py                       # _restore_from_snapshot v1 compat; sole _invoke_multi_session entry; single derivation site
├── dashboard.py                    # READ MIGRATION; SSE payload still emits top-level current_card
├── state_store.py                  # v1 forward-compat (already done in Fix 7 — verify)
└── services/
    ├── persona_service.py          # READ MIGRATION audit
    └── pr_checks_service.py        # READ MIGRATION audit

tests/
├── unit/
│   ├── graph/nodes/
│   │   ├── test_check_board.py             # Update single-card-mode-empty-sessions assertions
│   │   └── test_check_board_multicard*.py  # Parameterise across max_concurrent_cards=1 and >1
│   └── test_state_store.py                 # Extend v1 forward-compat
├── integration/
│   └── test_065_*.py                       # Must pass unchanged
└── e2e/
    └── test_dashboard_browser.py           # Verify SSE schema unchanged
```

**Structure Decision**: Single project (`src/coordinare/` + `tests/`).
No new directories. The refactor is dense in `graph/nodes/` and
`daemon.py`; the remaining edits are read-site migrations across the
existing module layout.

## Phase 0 — Outline & Research

Produced in [./research.md](./research.md). Findings to capture:

1. **Current divergences catalogued** — A line-by-line audit of `check_board.py` listing each `if max_concurrent_cards > 1` (or equivalent) branch and what each side does. Output: a table mapping branch → behaviour → which FR governs the unified result.
2. **Read-site inventory** — `grep -rn 'state\["current_card"\]\|state\.current_card\|state\.get("current_card")' src/ tests/` to enumerate every direct read of `current_card`. Each row gets a "migrate / leave (test fixture) / leave (single derivation site)" disposition.
3. **v1 snapshot probe** — Verify Fix 7's v2 schema is the current persisted shape; capture a sample v1 (pre-Fix 7) snapshot synthetically and assert the rehydration plan handles it. If a real v1 snapshot is recoverable from operator backups, use it.
4. **`_invoke_multi_session` audit** — Confirm it can take a 1-session input set without behavioural surprise. Document any guards it currently has that assume N>1.
5. **IN_REVIEW re-adopt audit** — Locate the existing IN_REVIEW pickup branch (search "IN_REVIEW" in `check_board.py`) and document how its phase derivation differs from IN_PROGRESS; design the unified status-aware phase derivation.
6. **Derivation contract** — Define exactly when and where the top-level `current_card` mirror is re-derived per cycle. Single-site rule: only `check_board` (or the daemon after `check_board`) mutates the mirror.

**Output**: research.md with all six items resolved. No `NEEDS CLARIFICATION` markers remain.

## Phase 1 — Design & Contracts

### data-model.md

Documents the unified `CoordinareState` shape post-066:

- `active_sessions: dict[str, CardSession]` — authoritative
- `active_card_id: str | None` — index pointer (already present; document invariants)
- `current_card: dict | None` — **derived mirror** of `active_sessions[active_card_id]` when present; `None` when no card is in flight
- Re-derivation contract: invoked exactly once per `check_board` cycle, after session-set mutation
- Status-aware phase derivation table: `(card.status, session.performer_stage) → phase`

### contracts/

Internal contract document — no HTTP/GraphQL surface:

- `contracts/current_card-derivation.md`: spec for the single derivation function, its inputs/outputs, and the invariant that no other site may mutate `state.current_card`. This is the contract the read-site migration relies on.

### quickstart.md

Short operator-facing rollout notes:

- "No config change required."
- "v1 snapshots auto-migrate on first daemon start post-merge."
- "Dashboard SSE schema unchanged; clients require no update."
- "If you previously relied on `max_concurrent_cards: 1` for behavioural reasons other than concurrency, file an issue — the unified path is now the only path."

### Agent context update

Run `.specify/scripts/bash/update-agent-context.sh claude` to refresh
the project's `CLAUDE.md` Active Technologies section with no new tech
(the refactor adds no dependencies).

## Phase 1 Constitution Re-check

| Principle | Re-check | Notes |
|---|---|---|
| I. Code Quality First | ✅ | Derivation contract narrows mutation surface to one site. Net branching down. |
| II. Testing Discipline | ✅ | Parameterising existing multi-card tests across N=1 and N>1 increases effective coverage without LOC bloat. |
| III. UX Consistency | ✅ | SSE schema preserved. |
| IV. Performance by Design | ✅ | One extra dict lookup per cycle for the derivation; well inside the +50ms budget. |
| V. Clarity Before Action | ✅ | Phase 0 audit explicitly resolves every divergence before code lands. |

No drift. Plan proceeds to Phase 2 (`/speckit.tasks`).

## Complexity Tracking

*No constitution violations; section intentionally empty.*

## Stop Condition

This plan ends at Phase 2 entry. `/speckit.tasks` produces `tasks.md`;
`/speckit.analyze` gates `/speckit.implement` per project convention.

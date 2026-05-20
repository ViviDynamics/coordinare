# Phase 0 Research: Unify Card Pickup Paths

**Spec**: [./spec.md](./spec.md) | **Plan**: [./plan.md](./plan.md)

## 1. Divergence Audit — `check_board.py`

The two pickup branches are gated on `config.max_concurrent_cards`. Audit
of the file (post-065, at the time of writing) identifies the following
divergence sites and their unified resolution:

| Line(s) | Branch behaviour | Unified resolution |
|---|---|---|
| 174 | `_raw_max = getattr(config, "max_concurrent_cards", 1)` — used to gate the multi-card pre-poll throttle | Keep; throttle remains gated on N > 1 (legitimate optimisation, not pickup logic) |
| 292–315 | Single-card IN_PROGRESS re-adoption: sets `state["current_card"]` directly | Delete; replaced by the multi-card IN_PROGRESS re-adoption at L539–585 with N=1 |
| 391–467 | IN_REVIEW re-adopt (multi-card) | Preserve; this becomes the *only* IN_REVIEW re-adopt path (FR-009) |
| 476–520 | Single-card early-return for `dispatching | monitoring_performer | blocked` phases | Delete; multi-card already handles per-session phase derivation correctly (Fix 5 lesson) |
| 590–707 | Single-card TODO pickup + un-block detection | Delete; multi-card path at L1029+ already does both. Port the US4 un-block reset side-effect (currently only on this branch) into the multi-card path |
| 1029–1194 | Multi-card pickup (the surviving path) | Keep; folds in single-card's un-block reset side-effect and the L292 IN_PROGRESS re-adopt logic with N=1 |

**Lesson from 065**: Every fix delivered to this file in the last three
cycles (Fix 2, Fix 5, US4) was a port from the surviving branch to the
removed one. The unification eliminates the "did you also patch the
other branch?" question by construction.

## 2. Read-Site Inventory — `current_card`

A `grep` across `src/coordinare/` and `tests/` returns ~50 read sites
and ~20 write sites of `state["current_card"]`. Disposition:

- **Read sites (no migration required)**: read sites can continue to
  read `state.current_card` because FR-010 keeps the field as a derived
  mirror. The migration is a *write-site* migration, not a read-site
  migration. This is the key insight that bounds the blast radius.
- **Write sites OUTSIDE check_board / daemon derivation**: every one
  is a bug under FR-010 and MUST be migrated to mutate the session
  entry instead, after which the daemon re-derives the mirror.

Write-site disposition table (top sites only — `tasks.md` will
enumerate the rest):

| File:line | Current behaviour | Migration |
|---|---|---|
| `check_board.py:315, 632, 707, 1169, 1194` | Sets `current_card` during pickup | Replaced by session mutation + single re-derivation at end of `check_board` |
| `check_board.py:775–776` | Mutates `previous_status` / `status` during Q&A-answer detection | Mutate session entry instead (FR-011); re-derivation propagates to mirror |
| `dispatch_performer.py:168, 236, 632, 718` | Sets `current_card` to track dispatch progress | Mutate session entry; re-derive before return |
| `monitor_performer.py:90, 105, 124, 1125, 1534` | Sets `current_card` to None on terminal transitions, populates on others | Update session (or remove entry); re-derive |
| `monitor_pr.py:83, 128` | Sets `current_card` on PR updates | Update session; re-derive |
| `merge_pr.py:77` | Sets `current_card` post-merge | Update session; re-derive |
| `assess_card.py:170`, `classify_human_feedback.py:340` | Sets `current_card` after node-local mutation | Update session; re-derive |
| `daemon.py:393, 499, 773, 1075` | Restore/cleanup writes from daemon orchestration | Migrate to update session; the daemon owns re-derivation as part of `_invoke_multi_session` post-step |

The pattern is consistent: every write of `current_card` becomes a
write of `active_sessions[active_card_id]` plus a re-derivation. The
re-derivation is a one-liner; the migration is mechanical.

## 3. v1 Snapshot Compatibility

`state_store.py` (Fix 7) introduced v2 snapshots that persist
`active_sessions`. v1 snapshots have only `current_card` set.

Decision: `_restore_from_snapshot` (currently at `daemon.py:393–500`
range) MUST detect v1 by absence of `active_sessions` and synthesize a
single-entry session map from `current_card`. The synthesized session
adopts `active_card_id = current_card.id`. This makes the unified code
treat post-restore v1 state as if the card had been picked up via the
multi-card path. Existing tests in `tests/unit/test_state_store.py`
already cover v1 round-trip; one new case asserts the synthesized
`active_sessions` shape.

## 4. `_invoke_multi_session` with N=1

Audit: `_invoke_multi_session` already iterates the session set and
invokes the graph per-session. There is no N>1 guard. The
"multi-card pre-poll throttle" (line 174) is a legitimate optimisation
gated on N>1 and stays gated; it is not pickup logic.

Decision: no changes required to `_invoke_multi_session` itself.
Single-card mode becomes "the same loop, length-1."

## 5. IN_REVIEW Re-adopt Audit

The existing IN_REVIEW re-adopt branch (L391–467) is structurally
identical to the IN_PROGRESS branch (L539–585). The differences are:

- Initial `status` and `previous_status` are set to `"IN_REVIEW"` instead of `"IN_PROGRESS"`
- Subsequent dispatch in the same cycle is skipped (line ~474 comment: "the dispatch will move it on the next attempt")
- The session's `performer_stage` defaults to a monitoring stage rather than a dispatch stage

These differences are status-dependent, not control-flow-dependent.
The unified pickup function takes the card's GitHub status as input
and uses a small lookup table to set `status`, `previous_status`, and
the initial `performer_stage`. Both branches collapse to one function
parameterised by the status the card holds on the board.

## 6. Derivation Contract for `current_card`

**Rule**: `state["current_card"]` is mutated in exactly one place:
the end of `check_board.py`'s top-level node function, immediately
after session mutations are complete. Every other site that previously
wrote `current_card` instead writes
`state["active_sessions"][state["active_card_id"]]`.

**Derivation**:
```text
def _rederive_current_card(state):
    sessions = state.get("active_sessions") or {}
    active_id = state.get("active_card_id")
    if active_id and active_id in sessions:
        state["current_card"] = sessions[active_id].get("current_card")
    else:
        state["current_card"] = None
```

**Invocation points**: end of `check_board`, end of each branch of
`_invoke_multi_session` per-session inner loop (so downstream nodes
within the same per-session graph still see the mirror they expect),
and end of `daemon._restore_from_snapshot`.

**Test invariant**: after every state mutation in a per-session graph
step, `state["current_card"]` equals `state["active_sessions"][state["active_card_id"]]["current_card"]` (or both are None). A
fixture-level assertion can enforce this in the existing test suite
without per-test changes.

## Resolved Items

- [x] Scope (wide) — confirmed by operator 2026-05-20
- [x] Read-site blast radius bounded — derivation mirror eliminates most read-site changes
- [x] v1 snapshot compatibility strategy — synthesize sessions from `current_card`
- [x] IN_REVIEW unification approach — status-parameterised single function
- [x] `_invoke_multi_session` is unchanged for N=1
- [x] Single derivation site and invocation points identified

No `NEEDS CLARIFICATION` markers remain.

# Implementation Plan: Terminal-Success Progress Floors

**Branch**: `126-terminal-success-floors` (stacked on `125-stage-verdict-memory`) | **Date**: 2026-07-04 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/126-terminal-success-floors/spec.md`

## Summary

Coordinare verifies performer *failure* claims (070/072 zero-progress guards) but trusts *success* claims unconditionally. This feature adds role-aware progress floors on terminal success: (US1) an implementer completing a feedback-driven dispatch must have moved the PR head past the **feedback-origin SHA** — the head the feedback was raised against — or explicitly dispute items; (US2) feedback items get stable IDs, per-item dispositions (`addressed`/`disputed`) returned on the completion contract, dispute adjudication by the raising stage, and per-raiser re-raise tracking; (US3) a tech_writer `docs_committed` with zero modified files and no head delta advances but is never recorded as a documentation pass (which keeps 125's last-documented SHA truthful). Floor loops consume a bounded no-op allowance, never content-feedback budget. Snapshot schema v13 → v14.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — monitor_performer terminal-success block + bounce/feedback sites (`changes_requested` handler ~:3520, CI-gate bounce `bounce_updates` ~:2340, `security_failed`/`qa_failed` handlers), the 070/072 zero-progress patterns (`monitor_performer.py:4298-4340` for the strengthened-re-dispatch shape), `relay_feedback` passthrough (already `list[dict]` on the dispatch payload and performer `Score.relay_feedback` — `agent/performer/src/performer/models.py:117`; adding keys inside entries is contract-compatible), `ProtocolResponse` on both sides (`src/coordinare/protocol.py`, `agent/performer/src/performer/protocol.py`) for the new `feedback_dispositions` response field, the implementer persona (`services/persona_service.py`), 125's `_verdict_cache_check` (FR-012 dispute veto) and `_record_stage_verdict` (US3 no-op suppression). No new external dependencies.
**Storage**: JSON snapshot via `state_store.py` — schema v14 adds on `PersistedSession`: `feedback_ledger` (list of feedback-item records: id, raiser, origin_sha, body-digest, disposition, dispute_reason, re_raised, status), `feedback_origin_sha: str | None`, `noop_success_retries: int`. All defaults empty/None/0; v1–v13 snapshots load unchanged.
**Testing**: pytest — unit tests per contract table; contract test for v13→v14 (mirrors v12→v13); performer-side unit tests for disposition extraction (`agent/performer/tests/`).
**Target Platform**: single-host coordinare daemon + performer container (protocol addition is backward-compatible: absent dispositions = none disputed).
**Project Type**: single project (coordinare + colocated performer package)
**Performance Goals**: zero new API calls; floor evaluation is in-memory comparisons. Reliability budget: a head-unmoved no-op success costs ≤1 strengthened re-dispatch before an operator hold (today: up to 5 full downstream laps × content budget).
**Constraints**: verdict roles (reviewer/security/qa/closer) exempt from head floors (FR-009); floors fail open on missing bookkeeping (FR-011); floor loops never increment content/transient budgets (FR-003); dispute of CI-raised items routes to hold, never auto-acceptance (FR-007); dispute queued for a stage vetoes 125's verdict-cache skip (FR-012).
**Scale/Scope**: ~6 coordinare files + 2 performer files (protocol + main extraction) + persona text; ~30 tests. Deliberate simplification (documented): re-raise tracking is **per-raiser round**, not per-item text matching — when a raising stage bounces again while its disputes were pending, the new items are marked `re_raised` and a second head-unmoved dispute for that raiser routes to the operator hold. Item-level semantic matching of re-raised feedback is explicitly out of scope.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First**: PASS — one floor evaluator, one ledger stamp helper, one protocol field; reuses zero-progress machinery rather than a parallel loop.
- **II. Testing Discipline**: PASS — TDD against the contract tables (floor rows, ledger rows, disposition rows); v13→v14 contract test; performer extraction tests; deterministic stubs only.
- **III. UX Consistency**: PASS — floor events follow existing `monitor_performer.*` naming; operator hold uses the existing blocked/notify surfaces.
- **IV. Performance by Design**: PASS — no new I/O; ledger bounded (items for the current + previous feedback round only, superseded rounds pruned).
- **V. Clarity Before Action**: PASS — zero NEEDS CLARIFICATION; the per-raiser re-raise simplification and weak-model tolerance (missing dispositions never hard-block when the head moved) are recorded as deliberate decisions.

**Post-Phase-1 re-check**: PASS — no violations; Complexity Tracking empty.

## Project Structure

### Documentation (this feature)

```text
specs/126-terminal-success-floors/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── success-floors.md      # floor + ledger + disposition decision tables
│   └── state-schema-v14.md    # persisted fields + migration contract
└── tasks.md
```

### Source Code (repository root)

```text
src/coordinare/state_store.py                     # v14: FeedbackItemRecord; PersistedSession.{feedback_ledger, feedback_origin_sha, noop_success_retries}
src/coordinare/session.py                         # CardSession fields + _SESSION_FIELDS
src/coordinare/daemon.py                          # persist/restore coercion
src/coordinare/graph/state.py                     # CoordinareState keys + defaults
src/coordinare/protocol.py                        # ProtocolResponse.feedback_dispositions
src/coordinare/graph/nodes/monitor_performer.py   # ledger stamping at bounce sites; success floor; dispute routing; US3 no-op completion; 125 record suppression
src/coordinare/graph/nodes/dispatch_performer.py  # dispute-context injection; FR-012 verdict-cache veto
src/coordinare/services/persona_service.py        # implementer persona: per-item dispositions required
specs/contracts/dispatch-payload.md              # registry: relay_feedback entry keys + disputed_feedback context field
agent/performer/src/performer/protocol.py        # ProtocolResponse.feedback_dispositions (mirror)
agent/performer/src/performer/main.py            # extract dispositions from the implementer report

tests/contract/test_state_persistence_v13_to_v14.py
tests/unit/graph/nodes/test_success_floor.py
tests/unit/graph/nodes/test_feedback_ledger.py
tests/unit/graph/nodes/test_documenting_noop_completion.py
agent/performer/tests/... (disposition extraction)
```

**Structure Decision**: single project; the floor evaluator (`_evaluate_success_floor`) and ledger helpers colocate in `monitor_performer.py` (single consumer, mirrors `_evaluate_ci_gate` placement).

## Key design decisions (full detail in research.md)

1. **Feedback-origin SHA, not dispatch-time head**: the floor compares `status.head_after` against the SHA the feedback was raised against (stamped at bounce time from the verdict's rollup/status head). Commits from a prior crashed session count as progress (spec Edge Cases).
2. **One ledger, stamped at bounce sites**: every path that queues implementer-bound feedback (`changes_requested`, CI-gate bounce, `security_failed`, `qa_failed`) routes through `_stamp_feedback_bounce(state, items, raiser, origin_sha)` which assigns ids (`fb-<n>`), sets `feedback_origin_sha`, and appends ledger records. relay_feedback entries carry `id`/`raiser`/`re_raised` inline (contract-compatible: `list[dict]`).
3. **Floor placement**: inside the implementing terminal-success branch, after the env-health taint check and BEFORE the CI gate (no point CI-gating a no-op). Trip ⇒ reuse the 070-style strengthened re-dispatch once (`noop_success_retries`), then operator hold. Never touches content/transient budgets.
4. **Dispositions are advisory-tolerant**: missing dispositions only matter when the head did NOT move — weak backends that omit them but commit real work are unaffected (FR-011 spirit).
5. **Dispute adjudication**: `disputed` ledger entries queue for the raiser; dispatch injects `disputed_feedback` into that stage's context; the stage's next verdict resolves them (pass ⇒ accepted; bounce ⇒ round marked rejected, new items `re_raised=true`). A second head-unmoved dispute against a `re_raised` round ⇒ hold. CI-raised items (`raiser="ci"`) skip adjudication: dispute ⇒ hold directly.
6. **FR-012 veto**: 125's `_verdict_cache_check` gains a dispute-queue check (V2b): pending disputes for a stage always dispatch it.
7. **US3 no-op completion**: `docs_committed` with empty `files_modified` AND no head delta ⇒ advance + `documenting_noop_completion` event + suppress 125's `_record_stage_verdict` (keeps the last-documented SHA truthful; the honest-no-op case is legitimate and never bounced).

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

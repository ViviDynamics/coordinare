# Implementation Plan: Stage Verdict Memory

**Branch**: `125-stage-verdict-memory` | **Date**: 2026-07-04 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/125-stage-verdict-memory/spec.md`

## Summary

Give coordinare a persisted, per-stage verdict memory keyed by PR head commit SHA so bounce cycles stop re-dispatching verdict stages (reviewing, security, qa, documenting, closing_review) whose input has not changed. Re-key the spec-123 documenting gate to "what changed since the last documentation pass" (a SHA-to-SHA compare) instead of the whole-PR diff, consolidate the duplicate `get_pr_diff` fetch on documenting dispatch, and persist the issue-comment dedup watermark so restarts stop re-classifying processed comments. One snapshot schema bump: v13 → v14.

Verdicts are **recorded** in `monitor_performer`'s terminal-success block (the only place passing verdicts are adjudicated); skips are **decided** in `_dispatch_performer_body` next to the existing persona-scope and doc-gate skips, comparing the recorded SHA against the *live remote head* (`check_mergeability().head_ref_oid` — never coordinare's own bookkeeping) so a human push can never be skipped over. Every uncertainty dispatches (fail-open).

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — pydantic 2.x (new `StageVerdict` submodel on `PersistedSession`), `monitor_performer` terminal-success handling (`monitor_performer.py:3324-3395`), `_dispatch_performer_body` skip-gate block (`dispatch_performer.py:738-784`), `github.check_mergeability` (returns `head_ref_oid`, `github.py:1476-1497`), `github.get_pr_diff` REST plumbing (`github.py:1153-1197`; the new SHA-compare endpoint reuses `_rest_api_base()`/`_current_token()`), `route_issue_comments` watermark (`route_issue_comments.py:41-42,125-126`), `state_store.py` snapshot (v13), `daemon.py` persist/restore. No new external dependencies.
**Storage**: single-host JSON snapshot via `state_store.py` — schema v14 adds three per-card `PersistedSession` fields: `stage_verdicts` (`dict[stage → {head_sha, verdict, recorded_at}]`), `processed_issue_comment_ids` (bounded `list[int]`) and `last_issue_comment_id` (`int | None`). v1–v13 snapshots load with empty defaults (FR-011). The "last documented SHA" is **derived**: `stage_verdicts["documenting"].head_sha` — no separate field.
**Testing**: pytest — unit (`tests/unit/graph/nodes/`, `tests/unit/test_state_store*.py`), contract (`tests/contract/` schema round-trip v13→v14), following the 123 v11→v12 test pattern.
**Target Platform**: single-host coordinare daemon (unchanged)
**Project Type**: single project (existing `src/coordinare/` layout)
**Performance Goals**: a cache hit saves a full performer dispatch (LLM + container + poll cycles). Cost of a miss: ≤1 GraphQL `check_mergeability` call per verdict-stage dispatch evaluation (shared between the verdict cache and the doc gate within one evaluation) and ≤1 REST compare call on documenting dispatch. Documenting dispatch drops from 2 `get_pr_diff` calls to 1 (SC-004).
**Constraints**: byte-identical behaviour with empty caches (FR-012 / SC-006); skip only on exact SHA match + passing verdict + no pending feedback/override (FR-002); implementing/assessing never skipped (FR-004); every skip observable and distinguishable from 123's `no_doc_changes` (FR-005).
**Scale/Scope**: ~5 source files touched (`state_store.py`, `session.py`, `daemon.py`, `monitor_performer.py`, `dispatch_performer.py`, `services/github.py`), 1 new pydantic submodel, ~25 tests.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First**: PASS — one submodel, one compare endpoint, one skip helper; no new deps; typed public surfaces.
- **II. Testing Discipline**: PASS — TDD throughout; contract tests for the v13→v14 round-trip mirror `tests/contract/test_state_persistence_v11_to_v12.py`; deterministic stubs for github service; no time-dependent assertions (`recorded_at` is observability-only, FR: skip decisions compare SHAs, never times).
- **III. UX Consistency**: PASS — skip events follow the existing `dispatch_performer.stage_skipped` shape with a new `reason` value; dashboard/log surfaces unchanged otherwise.
- **IV. Performance by Design**: PASS — explicit budget above; the feature is itself a performance feature (SC-001..SC-004 are the budget).
- **V. Clarity Before Action**: PASS — zero NEEDS CLARIFICATION markers; deliberate scope pins documented in spec Assumptions (docs/ predicate unchanged; persona/model changes don't invalidate the cache — override is the escape hatch).

**Post-Phase-1 re-check**: PASS — no violations introduced; Complexity Tracking empty.

## Project Structure

### Documentation (this feature)

```text
specs/125-stage-verdict-memory/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── state-schema-v14.md   # New persisted fields + migration contract
│   └── skip-decision.md      # Verdict-cache + doc-gate decision tables
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/state_store.py                     # v13: StageVerdict model; PersistedSession.stage_verdicts; WorkflowSnapshot.processed_issue_comment_ids/last_issue_comment_id
src/coordinare/session.py                         # CardSession.stage_verdicts + _SESSION_FIELDS plumbing
src/coordinare/daemon.py                          # persist/restore the new per-card fields
src/coordinare/graph/state.py                     # CoordinareState.stage_verdicts (per-card, restored into state)
src/coordinare/graph/nodes/monitor_performer.py   # record verdict on terminal success (stage↔marker map); override forced-dispatch flag in _apply_pending_override
src/coordinare/graph/nodes/dispatch_performer.py  # verdict-cache skip + re-keyed doc gate + shared _fetch_pr_data
src/coordinare/services/github.py                 # compare_changed_files(pr_url, base_sha, head_sha) via REST compare
src/coordinare/graph/nodes/route_issue_comments.py # (no logic change — state now persisted)

tests/unit/test_state_store.py                   # v13 fields round-trip + defaults
tests/contract/test_state_persistence_v13_to_v14.py  # migration contract
tests/unit/graph/nodes/test_dispatch_verdict_cache.py # skip decision table
tests/unit/graph/nodes/test_dispatch_doc_gate_sha.py  # re-keyed doc gate + shared fetch
tests/unit/graph/nodes/test_monitor_performer_verdict_record.py # recording sites
tests/unit/test_daemon_comment_watermark.py      # restart round-trip for US4
```

**Structure Decision**: single-project layout; all changes inside existing modules. The skip helper and stage↔marker map live in `dispatch_performer.py`/`monitor_performer.py` respectively (single consumer each); the only new cross-module surface is `github.compare_changed_files`.

## Key design decisions (Phase 0/1 summary — full detail in research.md)

1. **Live remote head, never local bookkeeping**: the skip compares the recorded verdict SHA against `check_mergeability().head_ref_oid` fetched at dispatch-evaluation time. Any fetch failure → dispatch (FR-003). This is what makes a false skip structurally impossible after any out-of-band push.
2. **Record on the marker that matches the stage**: `{"reviewing": "approved", "security": "security_passed", "qa": "qa_passed", "documenting": "docs_committed", "closing_review": "approved"}`. The recorded SHA is the performer-reported settled head (`status["head_after"]`, 072 audit-trail field), falling back to `status["head_sha"]`; no resolvable head → no record (never guess).
3. **Override veto via one-shot flag**: `_apply_pending_override`'s `restart` action sets `override_forced_dispatch = target_stage`; the skip check consumes and clears it, guaranteeing an operator-restarted stage always runs (US1 scenario 4).
4. **Doc gate ordering** (documenting stage only): (a) exact-SHA verdict-cache hit → skip; (b) prior doc pass exists → REST compare `S_doc...head`; no `docs/` path touched → skip (`no_doc_changes_since_last_pass`); (c) compare unavailable/failed → 123 whole-PR gate; (d) that too fails → dispatch. The mergeability call from (a) supplies the current head for (b) — one GraphQL call powers both.
5. **Shared fetch**: `_fetch_pr_data()` returns `(raw_diff, changed_files)` from one `get_pr_diff` call; the doc-gate fallback and the diff injection both consume it (SC-004). Failure semantics preserved per-consumer (gate fail-open; diff omitted with persona fallback).
6. **Comment watermark**: persist `processed_issue_comment_ids` (bounded: keep the numerically largest 2000 — GitHub comment IDs are monotonic, so largest = newest) and `last_issue_comment_id` **per-card on `PersistedSession`** (the comment router reads the active card's linked issue). `route_issue_comments` logic is untouched.

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

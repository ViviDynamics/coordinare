# Implementation Plan: Approval/Feedback Race — No Merge Over Unprocessed Feedback

**Branch**: `127-approval-feedback-race` | **Date**: 2026-07-04 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/127-approval-feedback-race/spec.md`

## Summary

`monitor_pr` currently lets a human APPROVED review win over actionable CHANGES_REQUESTED/COMMENTED reviews arriving in the same poll batch: the `if approved:` branch advances to merging and the `elif actionable:` relay branch never runs, permanently dropping the feedback (`src/coordinare/graph/nodes/monitor_pr.py:221-252`). The fix inverts the precedence — actionable feedback always routes to classification first; a coexisting approval is deferred implicitly (its review ID is never marked processed, so it re-surfaces and merges on a later evaluation once nothing actionable remains) — and adds per-reviewer latest-state supersession within the batch so a reviewer's own later review governs over their earlier one. No new persisted state; no schema change; one node function changes plus tests.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — `monitor_pr` node, `models/review.py` (`ReviewerType`, `classify_reviewer`), `github.get_pr_reviews` (returns `id`, `author_login`, `state`, `body`, `submitted_at`, `comments` — verified at `src/coordinare/services/github.py:1443-1474`), structlog. No new external dependencies.
**Storage**: N/A — no persisted-state change. The "deferred approval" is implicit: re-derived from live PR review state each evaluation (spec Key Entities). `processed_review_ids` (existing, persisted) remains the only bookkeeping.
**Testing**: pytest (`.venv/bin/pytest`), unit tests in `tests/unit/test_monitor_pr*.py` following the existing fixture style (stub github service returning canned review batches).
**Target Platform**: single-host coordinare daemon (unchanged)
**Project Type**: single project (existing `src/coordinare/` layout)
**Performance Goals**: none beyond baseline — the supersession pass is O(batch) over ≤50 reviews already in memory; zero additional API calls.
**Constraints**: behaviour must be byte-identical for approval-only and actionable-only batches (spec FR-006/SC-004), including the 090 baseline-prevention gate call order on the approval path.
**Scale/Scope**: one function (`monitor_pr` review-evaluation block, ~40 lines), one helper, structured log event, ~8 unit tests.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First**: PASS — single-purpose helper for per-reviewer supersession; no new dependencies; typed signatures.
- **II. Testing Discipline**: PASS — TDD: unit tests for each acceptance scenario (US1 ×6, US2 ×3) written first against the current buggy behaviour; deterministic stub github service; no flaky timing.
- **III. UX Consistency**: PASS (N/A UI) — the new `monitor_pr.merge_deferred` structlog event follows the existing `monitor_pr.*` event naming so operators see a consistent surface.
- **IV. Performance by Design**: PASS — no new I/O; in-memory grouping of an already-fetched ≤50-review batch. No budget change.
- **V. Clarity Before Action**: PASS — spec has zero NEEDS CLARIFICATION markers; ambiguous stale-approval policy is explicitly out of scope (spec Assumptions).

**Post-Phase-1 re-check**: PASS — design introduces no violations; no Complexity Tracking entries needed.

## Project Structure

### Documentation (this feature)

```text
specs/127-approval-feedback-race/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── review-routing.md  # Decision-table contract for the evaluation batch
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
src/coordinare/graph/nodes/monitor_pr.py   # review-evaluation block (lines ~186-253): supersession + precedence swap + deferral log
tests/unit/graph/nodes/test_monitor_pr_approval_race.py  # new — US1/US2 acceptance scenarios
tests/unit/test_monitor_pr.py             # existing — regression guard (approval-only / actionable-only unchanged)
```

**Structure Decision**: single-project layout, existing module; the entire change lands inside `monitor_pr`'s review-evaluation block plus one pure helper (`_latest_reviews_per_author`) colocated in the same module (it has no other consumers).

## Design decisions (Phase 0/1 summary)

1. **Precedence swap, not new state** — route `if actionable: → relay` before `elif approved: → merging`. The deferred approval needs no flag: its review ID is only added to `processed_review_ids` by `classify_human_feedback` when it rides `pending_reviews`, and approvals never ride `pending_reviews`; therefore an unprocessed approval re-surfaces on every subsequent poll until either it governs (batch has nothing actionable → merge) or the lifecycle cutoff filters it after a code-changing bounce (spec Edge Cases — branch protection owns stale-approval policy).
2. **Per-reviewer supersession** — group the *filtered* batch (post-cutoff, post-processed) by `author_login`, keep each author's latest by `submitted_at` (ISO-8601 strings from GraphQL; parse with the same `fromisoformat` pattern already used for the cutoff). Ties or unparseable timestamps resolve conservatively: the actionable state governs (spec Assumptions).
3. **Approval-path invariants preserved** — the 090 `_evaluate_baseline_prevention_gate` call stays exactly where it is, on the (now `elif`) approval branch, so approval-only batches are byte-identical (FR-006).
4. **Observability** — one structured event `monitor_pr.merge_deferred` `{card_id, approval_review_id, actionable_review_ids}` emitted when an approval coexists with unprocessed actionable reviews (FR-005 / US1 scenario 6).

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

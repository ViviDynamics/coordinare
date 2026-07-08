# Tasks: Stale / Addressed Human Review Handling

**Feature**: `128-stale-review-handling` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no incomplete-task dependency)
- **[Story]**: US1 / US2 / US3 (user-story phases only)
- Tests included per the constitution's NON-NEGOTIABLE testing discipline (TDD for the pure evaluator).

## Path Conventions

Single-project coordinare layout: `src/coordinare/...`, performer under `agent/performer/src/performer/...`, tests under `tests/unit/...` and `agent/performer/tests/unit/...`.

---

## Phase 1: Setup

- [x] T001 Confirm branch `128-stale-review-handling` and a green baseline before changes: `env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW .venv/bin/pytest -q` and `.venv/bin/ruff check src/coordinare` pass.

---

## Phase 2: Foundational (blocking prerequisites — shared by US1 & US3)

**Goal**: the data plumbing + the pure staleness evaluator that every story depends on.

- [x] T002 [P] Extend `src/coordinare/models/review.py`: add `commit_oid: str = ""` to `Review`; add `ReviewThread` model (`id: str`, `is_resolved: bool`, `review_id: str | None`); add `StalenessClass` enum (`FRESH`, `STALE_ADDRESSED`, `STALE_UNADDRESSED`).
- [x] T003 [P] Add `EventType.stale_review_surfaced` (severity `warning`) in `src/coordinare/models/notification.py`, matching the payload in `contracts/review-actions.md`.
- [x] T004 [P] Add `surfaced_stale_reviews: dict[str, str]` (default `{}`) to `PersistedSession` in `src/coordinare/models/session.py`; bump `CURRENT_SCHEMA_VERSION`; ensure prior snapshots load with the empty default (`state_store.py` / `daemon.py` restore path).
- [x] T005 Extend `GET_PR_REVIEWS_QUERY` and `get_pr_reviews()` parsing in `src/coordinare/services/github.py` to return per-review `commit_oid` (`PullRequestReview.commit.oid`) and the PR's `review_threads` (`id`, `is_resolved`, attributed `review_id`); default `commit_oid=""` / `threads=[]` on missing fields; log truncation when `reviewThreads` exceeds the page cap and treat the unknown remainder as unresolved.
- [x] T006 [P] Contract test `tests/unit/services/test_github_reviews.py`: mocked GraphQL response with `commit{oid}` + `reviewThreads` (mixed `isResolved`) → parsed `commit_oid` + threads with `review_id`; older-shape/partial response parses without raising; pagination-cap logs.
- [x] T007 [P] TDD tests FIRST in `tests/unit/test_review_staleness.py`: `FRESH` (review on head / unresolved threads on head), `STALE_ADDRESSED` (behind head + all threads resolved), `STALE_ADDRESSED` body-only (#111 shape: behind head, no threads, new commits), `STALE_UNADDRESSED` (behind head + some threads open), threshold boundaries (exactly-at vs past).
- [x] T008 Implement the pure evaluator `classify_review_staleness(review, head_oid, commits_behind, threads, config)` in `src/coordinare/services/review_staleness.py` (FR-001/FR-002/FR-007; deterministic; no I/O; config threshold) — make T007 pass.

---

## Phase 3: User Story 1 — Addressed change-request → re-request + IN_REVIEW (Priority: P1) 🎯 MVP

**Goal**: a stale-addressed human change-request re-requests the reviewer and routes the card to IN_REVIEW (not silent BLOCKED), with one deduped notification.
**Independent test**: on a PR whose only gate is a human CHANGES_REQUESTED on a commit N+ behind head with all threads resolved, one cycle → card IN_REVIEW + re-review requested + one notification; re-run → no duplicates.

- [x] T009 [P] [US1] Add `request_reviews(pr_id, reviewer_logins)` GraphQL mutation (union:true; resolve login→node id) in `src/coordinare/services/github.py`; fail-safe (permission/API error → return failure, never raise). Explicitly do NOT add any dismiss/approve mutation.
- [x] T010 [US1] TDD test in `tests/unit/graph/nodes/test_monitor_pr.py` (mocked github): STALE_ADDRESSED → routes card IN_REVIEW, calls `request_reviews` exactly once, emits one `stale_review_surfaced`; re-eval with unchanged head → no duplicate re-request/notification; mocked permission error → card stays parked, no raise; **(FR-011)** once the gating review is later approved/dismissed the card proceeds via the normal merge path and its `surfaced_stale_reviews` entry is cleared; **(self-approval guard, spec Edge Case)** assert `request_reviews` never results in a bot/self approval and no dismiss/approve mutation is ever called.
- [x] T011 [US1] Implement the `monitor_pr` branch in `src/coordinare/graph/nodes/monitor_pr.py`: call the evaluator; on `STALE_ADDRESSED` → `request_reviews` + route the card to IN_REVIEW **via the existing board state-transition helper the node already uses for column moves** + notify, deduped via `surfaced_stale_reviews[review_id]=head_oid`. **(FR-011)** When the gating review is subsequently approved/dismissed (reviewDecision no longer CHANGES_REQUESTED), clear the card's `surfaced_stale_reviews` entry so it resumes the existing normal merge path with no special-casing — make T010 pass.
- [x] T012 [US1] Emit the `stale_review_surfaced` notification via `notify.py` with dedup key `stale_review:{pr}:{review}:{head}` and the full payload (reviewer, dates, review-commit vs head, PR/review ids, next-action string); test dedup in `tests/unit/test_notify.py`.
- [x] T013 [US1] `STALE_UNADDRESSED` handling in `monitor_pr.py`: keep parked, emit one notification (no re-request, no IN_REVIEW flip); unit test the distinction from STALE_ADDRESSED.

**Checkpoint**: US1 independently delivers the core fix (the #111 class no longer silently blocks).

---

## Phase 4: User Story 3 — Fresh change-requests unchanged (Priority: P1)

**Goal**: genuinely-outstanding feedback keeps today's behavior (no regression).
**Independent test**: PR with human CHANGES_REQUESTED on the current head + unresolved threads → one cycle leaves existing behavior intact.

- [x] T014 [P] [US3] Regression test in `tests/unit/graph/nodes/test_monitor_pr.py`: FRESH classification (review on head / unresolved threads) → existing address-the-feedback path; stale path NOT triggered (no re-request, no premature IN_REVIEW).
- [x] T015 [US3] Verify/adjust `monitor_pr.py` so `FRESH` preserves the existing behavior exactly; make T014 pass without altering the non-stale code path's outcomes.

---

## Phase 5: User Story 2 — Implementer resolves threads it fixes (Priority: P2)

**Goal**: the implementer marks each inline thread it addressed resolved, producing the "addressed" signal.
**Independent test**: implementer run on a PR with open threads → addressed threads resolved with a note; unaddressed threads untouched.

- [ ] T016 [P] [US2] Add `resolve_review_thread(thread_id)` GraphQL mutation in `agent/performer/src/performer/github.py`; fail-safe.
- [ ] T017 [US2] Add the implementer step (in the performer's review-addressing path) to call `resolve_review_thread` for each addressed thread and post an "addressed in `<commit>`" reply; unit test in `agent/performer/tests/unit/`.
- [ ] T018 [P] [US2] Test that threads the implementer did NOT address remain unresolved.

---

## Phase 6: Polish & Cross-Cutting

- [ ] T019 [P] Add configurable staleness threshold (commits-behind and/or hours-behind) in the coordinare config models; the safe default MUST match research.md Decision 6 exactly — a review is stale only when its commit != head AND at least one later commit exists (never a review on the current head); document the knob.
- [x] T020 [P] Add structured `structlog` events for each decision (`stale_review.detected`, `.re_requested`, `.routed_in_review`, `.failsafe_skip`) in `monitor_pr.py`.
- [x] T021 [P] Operator docs: describe the stale-review behavior + threshold tuning (docs/operators/ and/or the living wiki).
- [ ] T022 Full suite green + coverage not decreased + `ruff` clean; run the `quickstart.md` live-smoke checklist against a #111-shaped PR.

---

## Dependencies & Execution Order

- **Phase 1** → **Phase 2** (foundational) blocks everything.
- **US1 (Phase 3)** depends on Phase 2 (evaluator T008 + data T002–T005).
- **US3 (Phase 4)** depends on the US1 `monitor_pr` branch (T011) — it's the FRESH side of the same decision.
- **US2 (Phase 5)** is independent of US1/US3 (performer-side) and can proceed in parallel once Phase 2 lands (needs `ReviewThread`/thread ids conceptually, but its mutation is self-contained).
- **Phase 6** last.

## Parallel Opportunities

- Phase 2: T002, T003, T004 in parallel (distinct files); T006 + T007 (tests) in parallel; T005 then T008.
- US1: T009 ∥ T010 (mutation vs test scaffolding); then T011 → T012 → T013.
- US2 (T016–T018) can run in parallel with US1/US3.
- Polish: T019, T020, T021 in parallel.

## Implementation Strategy

- **MVP = US1 (Phase 3)** on top of Phase 2 — delivers the core fix (no more silent forever-block for the #111 class).
- Then US3 (regression guard) to lock in "fresh unchanged", then US2 (implementer thread resolution) to strengthen the "addressed" signal, then Polish.
- Per project convention: run `/speckit.analyze` before `/speckit.implement`.

## Task Count

22 tasks (T001–T022) — Setup 1, Foundational 7, US1 5, US3 2, US2 3, Polish 4. Test tasks: T006, T007, T010, T013, T014, T017, T018 (+ dedup assertions in T012).


## Implementation notes (post-build)

- **T016–T018 (US2, implementer per-thread resolution): NOT built as a separate
  step — covered by the existing reviewer-stage `resolve_pr_review_threads`**
  (agent/performer/github.py), which bulk-resolves threads after verifying
  fixes. That already yields the "all threads resolved" signal US1's evaluator
  needs; a separate implementer mechanism would duplicate/conflict with it. The
  #111 body-only case needs no threads at all. Revisit only if per-thread,
  as-you-fix resolution is specifically wanted.
- **T019 (config-file threshold knob): default only.** `StalenessConfig`'s safe
  default (research Decision 6) is wired in the evaluator; exposing a config.yaml
  knob + precise commits-behind counting (needs a compare API) is deferred.
- **T022: full unit suite green (4444 passed) + lint clean + coverage not
  decreased.** The live #111-shaped smoke test (quickstart) is a manual step.
- Delivered + tested: T001–T015, T020, T021 (foundational + US1 + US3 + logging
  + docs). MVP is complete; adversarial review recommended before merge.

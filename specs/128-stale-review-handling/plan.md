# Implementation Plan: Stale / Addressed Human Review Handling

**Branch**: `128-stale-review-handling` | **Date**: 2026-07-08 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/128-stale-review-handling/spec.md`

## Summary

When a PR is gated by a human `CHANGES_REQUESTED` verdict whose feedback has since been addressed (inline threads resolved and/or the review sits on a commit far behind head), coordinare today leaves the card **silently BLOCKED** forever — GitHub never auto-clears a human change-request. This feature detects that "stale + addressed" condition in the PR-monitoring path and, instead of silent-blocking, **re-requests review from the original reviewer and routes the card to IN_REVIEW**, emitting one deduplicated operator notification. The implementer additionally **resolves inline review threads as it fixes each**, producing the "addressed" signal. Coordinare never auto-dismisses or auto-approves a human review (the human-approval gate is untouched); genuinely-fresh change-requests keep today's behavior.

**Technical approach**: extend the existing `get_pr_reviews` GraphQL query to return each review's associated **commit oid** and the PR's **review threads with `isResolved`**; add staleness/addressed derivation to the `Review` model + a small pure evaluator; branch `monitor_pr` on stale-addressed → re-request-review mutation + IN_REVIEW routing + a new deduped `EventType`; add an implementer-side step that calls `resolveReviewThread` for threads it addressed. All new outward GitHub calls fail safe.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — gql + `AIOHTTPTransport` (GitHub GraphQL, already used by `github.py`), langgraph (`monitor_pr` node), pydantic 2.x (`models/review.py`), structlog + the existing `notify.py` layer. No new external dependencies.
**Storage**: single-host JSON snapshot via `state_store.py`. A small per-card marker records that a given stale-review situation was already surfaced/re-requested (dedup across cycles), parallel to existing per-card review bookkeeping (`processed_review_ids`). Schema-version bump, backward-compatible default.
**Testing**: pytest (`tests/unit/`), including the proxy/graph node tests; contract test for the extended GraphQL query shape.
**Target Platform**: Linux server (coordinare daemon)
**Project Type**: single project (coordinare daemon + graph nodes + services)
**Performance Goals**: no per-cycle regression — the extended review fetch is one already-made GraphQL call with added fields (no extra round-trips in the common path); re-request + thread-resolve are one-shot per stale situation (deduped), not per cycle.
**Constraints**: MUST NOT auto-dismiss/approve a human review; MUST fail safe on missing bot permission / API error (card stays parked, cycle never crashes); MUST NOT re-notify or re-request on unchanged subsequent cycles.
**Scale/Scope**: small, surgical change to the review-evaluation path; a handful of files. No new services.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. Single-responsibility: staleness/addressed decision is a pure function in `models/review.py` (or a small `services/review_staleness.py`), separate from the `monitor_pr` orchestration and from the GitHub I/O. No new dependencies. Full type hints (pydantic models + typed helpers).
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. Pure evaluator gets exhaustive unit tests (stale/fresh/addressed/mixed-threads/body-only). `monitor_pr` branch gets unit tests with a mocked github service. The extended GraphQL query gets a contract test asserting the parsed shape (commit oid + thread `isResolved`). Coverage must not decrease; TDD for the evaluator (red-green-refactor).
- **III. User Experience Consistency** — PASS. Reuses the established `notify.py` event/severity/dedup pattern; the notification is actionable ("re-review or dismiss review #N on PR #M") per the error-communication rule. Card state transition (→ IN_REVIEW) follows existing board-state conventions.
- **IV. (Observability)** — PASS. Structured `structlog` events for each decision (stale detected, re-request sent, routed to IN_REVIEW, fail-safe skip), mirroring existing node logging.
- **V. Clarity Before Action** — PASS. The two scope-shaping decisions (re-request+IN_REVIEW vs auto-dismiss; implementer resolves threads) were resolved with the operator before planning; no unresolved ambiguity.

No violations → Complexity Tracking left empty.

## Project Structure

### Documentation (this feature)

```text
specs/128-stale-review-handling/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── get-pr-reviews.md        # extended GraphQL query + parsed shape
│   └── review-actions.md        # re-request-review + resolve-thread mutations
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── models/
│   └── review.py                 # + commit_oid on Review; ReviewThread; staleness/addressed derivation
├── services/
│   ├── github.py                 # extend GET_PR_REVIEWS_QUERY (commit oid + review threads isResolved);
│   │                             #   + request_reviews(pr_id, logins) + resolve_review_thread(thread_id)
│   ├── review_staleness.py       # NEW: pure evaluator — classify (fresh | stale-addressed | stale-unaddressed)
│   └── notify.py                 # + stale_review_surfaced event usage (dedup key per PR+review)
├── models/
│   └── notification.py           # + EventType.stale_review_surfaced
├── graph/nodes/
│   └── monitor_pr.py             # branch: stale-addressed → re-request + route IN_REVIEW + notify (deduped);
│                                 #   fresh → existing behavior
├── models/session.py (PersistedSession)   # + surfaced-stale-review marker (dedup across cycles); schema bump
└── (performer) agent/performer/src/performer/  # implementer step: resolve threads it addressed (US2)

tests/unit/
├── test_review_staleness.py      # pure evaluator: stale/fresh/addressed/mixed/body-only
├── graph/nodes/test_monitor_pr.py# branch behavior (mocked github): re-request+IN_REVIEW vs fresh unchanged
├── services/test_github_reviews.py # contract: extended query parsed shape (commit oid + isResolved)
└── test_notify.py                # dedup: one stale_review_surfaced per situation
```

**Structure Decision**: Single-project coordinare layout. The decision logic is isolated in a new pure `services/review_staleness.py` (testable in isolation, no I/O); GitHub I/O changes stay in `github.py`; orchestration/routing stays in `monitor_pr.py`; the persisted dedup marker rides `PersistedSession` (existing snapshot). The implementer-side thread resolution (US2) lives in the performer package.

## Complexity Tracking

*No constitution violations — section intentionally empty.*

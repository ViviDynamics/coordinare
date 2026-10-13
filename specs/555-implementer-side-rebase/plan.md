# Implementation Plan: Implementer-Side Rebase

**Branch**: `555-implementer-side-rebase` | **Date**: 2026-10-13 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/555-implementer-side-rebase/spec.md`

## Summary

Remove every daemon-side clone, rebase and push (`rebase_branch`, `force_push_with_lease`, `run_rebase_round`, `prepare_conflict_resolution` and their helpers in `services/rebase.py`) and their four call sites. Keep the read-only classification (`check_mergeability` plus `classify_pre_dispatch`) and the 096 anti-thrash marker. On divergence, a new `request_implementer_rebase` queues one relay-feedback entry and routes the card to the implementer. The pre-dispatch guard fails open on every path except the anti-thrash hold. The whole behavior sits behind `implementer_rebase.enabled` (default `false`).

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only. pydantic 2.x (one new config model), structlog, `github.check_mergeability` and `github.get_pr_review_context` (read-only GraphQL), langgraph nodes `dispatch_performer`, `check_board`, `merge_pr`. No new external dependency.
**Storage**: single-host JSON snapshot via `state_store.py`, unchanged. The existing `last_rebase_attempt` marker gains one outcome string (`delegated`); no schema bump.
**Testing**: pytest (`make test`), ruff (`make lint`), mypy (`make typecheck`)
**Target Platform**: Linux daemon container (Docker and Kubernetes)
**Project Type**: single
**Performance Goals**: no more hosting-platform reads per cycle than the 096 proactive sweep already made
**Constraints**: the daemon runs no git command that clones, rebases or pushes; dispatch must never wedge on the guard
**Scale/Scope**: 5 source files, 1 config model, about 10 test files

## Constitution Check

- **I. Code Quality First**: net deletion (about 350 lines of git plumbing leave the daemon). Pass.
- **II. Testing Discipline**: new unit tests for the feedback path and each fail-open branch; tests that asserted the daemon rebase round are rewritten, not deleted without replacement. Pass.
- **III. User Experience Consistency**: log vocabulary kept where behavior is unchanged; renames documented in `contracts/events.md`. Pass.
- **IV. Performance by Design**: no new per-cycle I/O. Pass.
- **V. Clarity Before Action**: two owner-visible decisions (merge instead of history rewrite; default-disabled key) are recorded in `research.md` and repeated in the PR. Pass.

Post-design re-check: no change.

## Project Structure

### Documentation (this feature)

```text
specs/555-implementer-side-rebase/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── divergence-feedback.md
│   └── events.md
└── checklists/requirements.md
```

### Source Code (repository root)

```text
src/coordinare/
├── config.py                         # + ImplementerRebaseConfig, ProjectConfiguration.implementer_rebase
├── models/rebase.py                  # + RebaseOutcome.DELEGATED; ephemeral clone paths removed
├── services/rebase.py                # git clone/rebase/push removed; + build_divergence_feedback,
│                                     #   request_implementer_rebase, implementer_rebase_enabled
├── graph/nodes/dispatch_performer.py # guard: classify, then request or proceed; _rebase_guard_run removed
├── graph/nodes/check_board.py        # edge round + proactive sweep collapse into one read-only sweep
├── graph/nodes/merge_pr.py           # post-merge rebase round removed (main SHA still recorded)
├── daemon.py                         # preflight rebase round removed (main SHA still seeded)
└── dashboard/templates/dashboard.html# "delegated" outcome label
tests/unit/
├── test_555_implementer_side_rebase.py   # new: feedback path, fail-open, no-git guarantee
└── (existing rebase tests updated)
specs/097-pre-dispatch-rebase/spec.md     # superseding note only
config.example.yaml                       # commented implementer_rebase block
```

**Structure Decision**: single project; the change stays inside the existing rebase service and its call sites. `services/rebase.py` keeps its path so imports and the mypy allowlist do not churn.

## Complexity Tracking

No constitution violations.

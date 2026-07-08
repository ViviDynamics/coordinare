# Implementation Plan: BLOCKED-Card Auto-Recovery + QA Visual-Capture Env Resilience

**Branch**: `129-blocked-card-recovery` | **Date**: 2026-07-08 | **Spec**: [spec.md](./spec.md)

## Summary

**US1**: in `check_board`, before skipping BLOCKED cards, re-evaluate each via a pure recovery evaluator; when its block condition has cleared, move it to the correct resumption column and notify once. **US2**: extend the QA verdict so "visual capture tooling unavailable" yields a recoverable env-blocked outcome (+ honest limited-pass), not a hard fail. All existing deps; no new externals.

## Technical Context

**Language**: Python 3.14 · **Deps**: existing only — langgraph (`check_board` node), pydantic, structlog, the spec-128 `review_staleness` evaluator, `github.get_pr_review_context`/`check_mergeability`, spec-095 `failure_classification.classify_failure_origin`, spec-120 `qa_verdict`, the `notify` layer. **Storage**: per-card persisted state (`PersistedSession`/`CardSession`) — add a small anti-thrash marker + block-reason record (schema bump, backward-compatible). **Testing**: pytest `tests/unit` + `tests/contract` (MUST run the whole tree — the schema/version contract tests live in `tests/contract`).

## Constitution Check

- **Code Quality**: recovery decision is a pure function in `services/blocked_recovery.py` (no I/O); check_board only orchestrates; QA change is localized to `qa_verdict.py`. PASS.
- **Testing (NON-NEGOTIABLE)**: pure evaluator + qa_verdict get exhaustive unit tests (TDD); check_board wiring gets mocked-github tests; schema/contract updated + `tests/contract` run. PASS.
- **UX**: reuses notify dedup pattern; recovery notification is actionable. PASS.
- **Observability**: structured events (`blocked_recovery.evaluated/recovered/still_blocked/failsafe`). PASS.
- No violations.

## Project Structure

```text
src/coordinare/
├── services/blocked_recovery.py     # NEW: pure evaluator — StillBlocked | Recover(target_stage, reason)
├── services/qa_verdict.py           # US2: capture-tooling-unavailable → env_blocked (recoverable) + honest limited-pass
├── graph/nodes/check_board.py       # US1: _attempt_blocked_card_recovery(...) hooked at the blocked-list (~L705), fail-safe, guarded
├── models/... (session/state_store) # block-reason record + anti-thrash marker; schema v16→17 (backward-compat)
└── models/notification.py           # + EventType.card_auto_recovered

tests/unit/test_blocked_recovery.py, tests/unit/test_qa_verdict_env_capture.py,
tests/unit/graph/nodes/test_check_board_recovery.py, tests/contract/ (schema/version bump)
```

**Structure Decision**: mirror spec-128 — isolate the decision logic in a pure, fully-tested service; keep the high-blast-radius `check_board` change to a small, fail-safe, feature-guarded hook; localize the QA change. Check the stale `fix/088-qa-env-blocked-and-daemon-blocked-recovery` branch for reusable groundwork before writing the wiring.

## Complexity Tracking

*No constitution violations.*

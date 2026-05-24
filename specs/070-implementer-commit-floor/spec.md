# Feature Specification: Implementer Commit-or-Checkpoint Contract

**Branch**: `069-blocked-notification-rehydration` (landed alongside 069; no dedicated branch)
**Status**: LANDED (commits `f83fb1c`, `92509f6`, `8262291`, `b735967`)
**Date**: 2026-05-23

> Retroactive spec — work was implemented under time pressure on the 069
> branch during a live operator session. This document captures the
> contract so future changes to the implementer protocol have a written
> reference.

## Problem

Implementer turns were exiting with prose "next steps" status reports
without pushing any commits. The coordinare then routed those exits
through the `blocked` fallback, surfacing them to the operator as if the
implementer needed clarification. Real failure mode observed on card #70
/ PR #135: implementer terminated cleanly but PR diff contained only
plan docs — no source-code changes.

Two independent gaps:

1. The implementer persona had no enforced terminal contract — DONE,
   PARTIAL_PROGRESS, and BLOCKED were not formally distinguished, so
   the model would emit "I have outlined the next steps…" and exit.
2. The coordinare had no guardrail: a `blocked` verdict from an
   implementer that never moved branch HEAD was honored verbatim,
   wasting an operator round-trip on an implementer self-loop.

## User Stories

### US1 — Implementer cannot exit DONE without commits (P1)

**As** an operator running implementer turns,
**I want** a DONE verdict to be impossible unless the branch HEAD has
actually advanced with source-code commits,
**so that** zero-progress turns surface as a re-dispatch instead of a
spurious blocked notification.

**Independent test**: Run an implementer turn that emits a `done`
verdict with `head_before == head_after`. Coordinare MUST route to
`dispatching` with a stronger directive, not to `blocked` or `closer`.

### US2 — Implementer can checkpoint long work without blocking (P1)

**As** an implementer agent working a multi-step task,
**I want** to push partial work and report `partial_progress` with a
`next_focus` description,
**so that** the coordinare relays my focus back into the next dispatch
without involving the operator.

**Independent test**: Implementer emits a trailing JSON sentinel
`{"status": "partial_progress", "next_focus": "..."}`. Performer pushes
commits, posts a PR comment, and returns `partial_progress`. Coordinare
re-dispatches `implementing` with the relayed focus.

### US3 — Non-implementer roles unaffected (P1)

**As** a reviewer / security / qa / docs persona,
**I want** my BLOCKED verdicts to continue routing to `phase=blocked`,
**so that** legitimate review blocks still reach the operator.

**Independent test**: Reviewer emits `blocked` with `head_before ==
head_after`. Coordinare MUST route to `blocked` (not re-dispatched).
Sentinel parser MUST be gated on `role == implementing`.

## Functional Requirements

- **FR-070-1** Implementer persona defines exactly three terminal
  outcomes: DONE (pushed + green CI), PARTIAL_PROGRESS (JSON sentinel),
  BLOCKED (operator input only). Status-report exits are forbidden.
- **FR-070-2** `ProtocolResponse` (performer + coordinare) carries
  `head_before`, `head_after`, and `next_focus` fields. `status` enum
  includes `partial_progress`.
- **FR-070-3** Performer captures branch HEAD at dispatch and at
  completion; both go on every terminal response.
- **FR-070-4** Performer parses the trailing
  `{"status": "partial_progress", ...}` JSON sentinel ONLY when
  `role == implementing`.
- **FR-070-5** On `partial_progress`, the performer pushes commits,
  posts a PR comment relaying `next_focus`, and returns
  `partial_progress`.
- **FR-070-6** Coordinare `monitor_performer` routes `partial_progress`
  back to `dispatching` with the relayed focus.
- **FR-070-7** Coordinare zero-commit guardrail: when
  `stage == implementing` and `head_before == head_after`, route
  `blocked` verdicts back to `dispatching` with a stronger directive
  instead of honoring the verdict. Gated on `stage == implementing` so
  reviewer/security/qa/docs blocks are unchanged.
- **FR-070-8** Implementer persona's DONE definition requires the PR
  diff to contain the implementation — docs-only / `cards/`-only diffs
  do not satisfy DONE.
- **FR-070-9** All default personas in `DEFAULT_INSTRUCTIONS` use a
  consistent Role / Process / Output / Forbidden skeleton with markdown
  headings and fenced JSON contracts. JSON field names, the architect's
  `---TASKS---` separator, spliced `_CI_*` directives, and QA tokens
  pinned by tests are preserved verbatim.

## Out of Scope

- Performer-side adapter changes for non-implementer roles.
- Sentinel parsing for any role other than `implementing`.
- New conducting backends — guardrail uses existing state machinery.

## Success Criteria

- Card #70 / PR #135 reproduction: implementer that emits "next steps"
  prose without commits triggers a re-dispatch (not a blocked
  notification).
- Reviewer/security/qa contract tests still assert `blocked` reaches
  `phase=blocked` unchanged.
- Protocol contract test asserts `partial_progress` is a valid status.

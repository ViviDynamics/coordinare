# Implementation Plan: Assessor Role Workflow with a Structured Assessment Hand-off

**Branch**: `166-assessor-workflow` | **Date**: 2026-09-06 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/166-assessor-workflow/spec.md`

## Summary

Add the third consumer of the spec-164 role-workflow layer: an assessor workflow (intake, assess, gate, report) that reads the card, its clarifications, makes exactly one schema-validated model call, applies code-driven gate rules to bound the questions, and commits nothing. Coordinare persists the assessment on the card, clears it on assessor re-dispatch, and injects it only into the architecting stage where the spec-165 intake renders it as the first section the architect reads. The clarification loop is bounded by code: at most two questions per round, never re-asking an answered question, ready after two answered rounds with the rest recorded as assumptions. Default-off via `workflow: assessor`.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only. pydantic 2.x (assessment and gate-rule models), the 164 layer (`workflows/`, `Toolkit` with no command runner, `budget`, `schema_guard`), `Toolkit` imported without `command_runner`, spec-165 architect intake adapted to render assessment first. No new external dependencies.
**Storage**: single-host JSON snapshot via `state_store.py`; schema version 17 -> 18 adds `PersistedSession.assessment`, defaulting to `None`. Older snapshots load unchanged.
**Testing**: pytest (`tests/unit`, `tests/contract`, `agent/performer/tests` run separately); worktree runs need `PYTHONPATH=src:agent/performer/src`.
**Target Platform**: the performer container (Debian, `coordinare-performer:extra`) for the workflow; the coordinare daemon for the carrier and assessment persistence.
**Project Type**: single repository, two packages (coordinare, performer).
**Performance Goals**: assessor round under 5 minutes p90 on the live fleet including container start (SC-001); assess call 3000 completion tokens with one reprompt (FR-002).
**Constraints**: the assessor writes nothing to the working tree; no force push anywhere (reuse 165's push-path change); the prose path is byte-for-byte unchanged with the flag off.
**Scale/Scope**: one new workflow package (about 7 modules), a persisted field, one payload field, one architect intake edit, an eval with three fixtures, gate rules with mutation-test coverage.

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Code Quality First | No dead code: every gate rule (FR-006 through FR-009) is a pure function with its own test; the toolkit is wired on day one without command runner. No new dependencies. | PASS |
| II. Testing Discipline | Unit tests per step (intake, assess, gate, report) and per gate rule; each rule test is shown to fail under a mutation of that rule (standing rule, Constitution II). Contract test for the assessment field in dispatch payload. Deterministic eval with three fixtures (clear, ambiguous, answered) and a stubbed model in CI; live eval on demand only. | PASS |
| III. UX Consistency | Operator surface is one key (`workflow: assessor`) matching 164/165; block reasons name the failing field (existing pattern). | PASS |
| IV. Performance by Design | Budgets stated in spec table (3000 tokens per assess call, under 5m p90 including container start). Provisional, replaced by SC-001's measurement from live logs. | PASS |
| V. Clarity Before Action | Every decision in research.md cites the code path (line numbers in workflows/toolkit.py, monitor_performer.py, dispatch_performer.py, etc.) or measurement it rests on. | PASS |

No gate changed. Post-design re-check (after data-model and contracts): ready.

## Project Structure

### Documentation (this feature)

```text
specs/166-assessor-workflow/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── assessment.schema.json          # the assessment as JSON Schema (shown to model)
│   └── dispatch-payload-additions.md   # rows to add to specs/contracts/dispatch-payload.md
├── checklists/requirements.md
└── tasks.md                            # produced by /speckit.tasks
```

### Source Code (repository root)

```text
agent/performer/src/performer/
├── workflows/
│   ├── __init__.py                     # + "assessor": (workflows.assessor, "AssessorWorkflow")
│   ├── _text.py                        # NEW: match_with_overlap(q1, q2, threshold) pure function
│   ├── assessor/
│   │   ├── __init__.py                 # AssessorWorkflow: intake -> assess -> gate -> report
│   │   ├── models.py                   # Assessment, GateRecord, ClarificationRound (bounded)
│   │   ├── personas.py                 # ASSESS step persona
│   │   ├── intake.py                   # assemble card and clarifications, count answered rounds (no model call)
│   │   ├── assess.py                   # the schema-guarded model call
│   │   ├── gate.py                     # gate rules FR-005 to FR-009 (pure functions, mutation-tested)
│   │   └── report.py                   # PerformerResponse.report shape + write-free check
│   └── toolkit.py                      # NOTE: no change; already allows command_runner=None
├── backends/_card_docs.py              # NOTE: no change (no documentation brief for assessor)
├── models.py                           # Score: + assessment (dict | None)
├── main.py                             # assessing post-processing: no workflow -> assessment.md; workflow -> assessment_complete
└── workspace.py                        # NOTE: no change (push-path already changed in 165)

src/coordinare/
├── state_store.py                      # schema 18: PersistedSession.assessment (dict | None)
├── graph/nodes/monitor_performer.py    # lift report["assessment"] into the session
├── graph/nodes/dispatch_performer.py   # inject assessment into architecting payload only; reset_on_assessor_dispatch
├── graph/nodes/check_board.py          # NOTE: no change (assessment is not a side run)
├── eval/assessor_scenarios.py          # NEW: stubbed (CI) and --live eval runner over three fixtures
├── config.py                           # KNOWN_WORKFLOWS += "assessor"
└── [existing architect intake edit]    # architect/intake.py: render assessment first if present

tests/
├── unit/workflows/assessor/            # models, gate rules (mutation-tested), assess, intake, report, e2e with stub model
├── unit/graph/nodes/                   # lift, dispatch reset, assessment field guard
├── contract/test_dispatch_payload.py   # + assessment field (architecting-only)
└── eval/assessor_scenarios/            # fixtures (3), stub model, scoring, --live mode
```

**Structure Decision**: mirror 164/165 exactly. The assessor is a sibling workflow under `workflows/`; the carrier follows the blueprint path with lift in monitor and inject in dispatch; gate rules are pure functions per Constitution II. No side run (assessment does not trigger concurrent work). The workflow is stateless within a round; state is on the card session where assessment is persisted and reset.

## Complexity Tracking

| Item | Why it is needed | Simpler alternative rejected because |
| --- | --- | --- |
| Gate rules as pure functions | Constitution II requires every rule with its own test; mutation testing is mandatory (standing rule). Bundling them into one "process model output" function hides which rule failed and makes mutation testing impossible. | A monolithic gate function cannot be tested or debugged per rule. |
| Assessment persisted on card | Architect must read assessment after restart (FR-012). Transient state (graph state only, like qa_findings) loses it. | Daemon restart mid-lifecycle would abandon the assessment and leave the architect without context. |
| Reset on assessor dispatch | A failed round must not leave stale assessment behind (spec 165 research #266). | Without reset, the architect could consume a stale assessment from a prior round if the next assessor round times out. |

## Phases

- **Phase 0 (research.md)**: complete; R1-R9 all resolved from code reads.
- **Phase 1 (design)**: data-model.md, contracts/, quickstart.md; agent context updated.
- **Phase 2 (tasks.md)**: produced by `/speckit.tasks`, organized by user story with gate rules and eval as separate phases.

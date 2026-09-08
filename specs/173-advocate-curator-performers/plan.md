# Implementation Plan: The advocate and the curator become performer runs

**Branch**: `173-advocate-curator-performers` | **Date**: 2026-09-07 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/173-advocate-curator-performers/spec.md`

## Summary

Two card-less performer roles replace one in-daemon service. The daemon gains a
per-cycle decision-and-dispatch pass for each role, copied in shape from the
wiki-init run that already does this in production: a hand-built `card_context`
with a synthetic id, a hand-synthesised `WorkspaceInfo(path=None, ...)`, a direct
`svc.dispatch_card(...)` that bypasses the graph node, a dedicated poll task, and
a completion handler writing symphony-scoped state. Inside the performer each
role is a spec-164 workflow with bounded steps, a call budget, schema-guarded
responses, and a gate in the 169/170/172 shape: a claim survives only when its
evidence is verbatim in material the run actually read.

`services/advocate.py`, `services/scoring.py`, the `advocate_scan` node and its
`START` edge retire. `check_board`'s label filter narrows to the escalation
label. Both personas are rewritten and three documents corrected.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; production on 3.14.5 via uv)
**Primary Dependencies**: existing only. pydantic 2.x (state and record models), the spec-164 workflow layer (`WorkflowAdapter`, `Toolkit`, `budget._STEP_BUDGETS`, `schema_guard.validate_with_reprompt`), httpx (the performer's GitHub calls), structlog, langgraph (a node is removed, none added). No new external dependency.
**Storage**: the existing single-host JSON snapshot via `state_store.py`. `EnvCacheState` / `EnvCacheStateSnapshot` gain per-role fields, exactly as the wiki-init gate did at schema v13. `CURRENT_SCHEMA_VERSION` bumps 19 to 20; v1 to v19 snapshots load with defaults and no migration step.
**Testing**: pytest. The two trees run in SEPARATE invocations (`tests` and `agent/performer/tests`), coordinare with `--cov=coordinare --cov-fail-under=90`. Every rule gets one named mutation applied in the real tree.
**Target Platform**: Linux container performers on a single-host daemon; macOS for development.
**Project Type**: single project, the existing `src/coordinare` and `agent/performer` layout.
**Performance Goals**: an advocate run completes within 6 minutes at the 90th percentile including container start; a curator run within 6 minutes; an issue matching a sensitive keyword costs zero model calls; a repository with nothing to do costs zero model calls.
**Constraints**: at most one run per role per repository in flight; a run makes no commit, no push and no pull request; the curator writes only to the backlog; a run's outcome is flushed to the snapshot rather than waiting on a lifecycle signature.
**Scale/Scope**: two roles, two workflow packages, one node and two services removed, one schema bump, roughly 20 files.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Verdict | Notes |
|---|---|---|
| I. Code Quality First | PASS | The retired path is deleted, not left dormant beside its replacement, which FR-026 requires and which the "no dead code" clause independently requires. Each workflow step is one function with one purpose. Public surfaces are typed; the records are pydantic models. |
| II. Testing Discipline | PASS | TDD, one named mutation per rule in the real tree, contract tests for the dispatch payload and the report shapes, integration tests for both runs end to end against a fake GitHub, and the 90% coverage floor. Deleting the retired path removes its tests too, so the coverage floor is checked against the post-deletion tree rather than assumed. |
| III. User Experience Consistency | NOT APPLICABLE | No user interface. The operator-facing surfaces are log events, notifications and issue comments, and the escalation reasons and notification shapes are preserved by FR-014 and FR-015. |
| IV. Performance by Design | **GAP, resolved** | The constitution requires performance budgets to appear in the spec's Success Criteria. My spec's criteria were all outcome-based, with no latency budget. Rather than justify the omission I am amending the spec to add SC-009 (a run completes within 6 minutes at the 90th percentile including container start) and SC-010 (a repository with nothing to do costs zero model calls). Both are measured in the live rounds, as specs 164 to 172 measured theirs. |
| V. Clarity Before Action | PASS | Six decisions were resolved with the operator before drafting and are recorded in the spec. This plan adds no `NEEDS CLARIFICATION`: every remaining choice had a defensible default grounded in an existing precedent, and each is recorded in research.md with the alternative it beat. |

No violations remain, so Complexity Tracking is omitted.

## Project Structure

### Documentation (this feature)

```text
specs/173-advocate-curator-performers/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/           # Phase 1
│   ├── cardless-dispatch.md
│   ├── advocate-record.schema.json
│   └── curation-record.schema.json
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2, not created here
```

### Source Code (repository root)

```text
agent/performer/src/performer/
├── workflows/
│   ├── __init__.py                 # + advocate, curator in SUPPORTED_WORKFLOWS
│   ├── budget.py                   # + step budgets for both roles
│   ├── advocate/
│   │   ├── __init__.py             # AdvocateWorkflow.run: intake, triage, classify, gate, act, report
│   │   ├── models.py               # IssueCandidate, Classification, AdvocateRecord, schemas
│   │   ├── intake.py               # open issues, minus those already labelled handled or escalated
│   │   ├── docs.py                 # read doc_sources from the checkout, record what was read
│   │   ├── triage.py               # sensitive-keyword rule, pre-model
│   │   ├── classify.py             # one guarded call per issue
│   │   ├── gate.py                 # citation resolves to a read doc; unsent judgements dropped
│   │   ├── act.py                  # label, comment, escalate
│   │   ├── personas.py
│   │   └── report.py
│   └── curator/
│       ├── __init__.py             # CuratorWorkflow.run: intake, filter, judge, gate, act, report
│       ├── models.py               # SelectionJudgement, CurationRecord, schemas
│       ├── candidates.py           # open issues minus already-on-board, by rule
│       ├── judge.py                # one guarded judgement per candidate
│       ├── gate.py                 # reason must quote the issue verbatim
│       ├── act.py                  # add to backlog, label, comment
│       ├── personas.py
│       └── report.py
├── github.py                       # + list_open_issues, add_labels, add_item_to_project
├── models.py                       # + Score.project_id
├── protocol.py                     # + advocate_complete, curation_complete
└── main.py                         # + two terminal branches BEFORE the shared tail

src/coordinare/
├── services/
│   ├── advocate.py                 # DELETED
│   ├── scoring.py                  # DELETED
│   ├── intake_dispatch.py          # NEW: the per-cycle decision for both roles
│   └── wiki_init.py                # unchanged, the pattern this copies
├── graph/
│   ├── builder.py                  # advocate_scan node and START edge removed
│   └── nodes/
│       ├── advocate.py             # DELETED
│       └── check_board.py          # filter narrowed to the escalation label
├── daemon.py                       # + dispatch and poll for both roles
├── models/env_cache.py             # + per-role gate fields
├── state_store.py                  # + persisted subset, schema 19 to 20
├── config.py                       # AdvocateConfig trimmed, CuratorConfig added
├── services/persona_service.py     # advocate persona rewritten, curator added
└── eval/{advocate,curator}_scenarios.py

tests/
├── unit/workflows/{advocate,curator}/
├── unit/services/test_intake_dispatch.py
├── integration/test_cardless_runs.py
├── contract/test_cardless_dispatch_payload.py
└── eval/{advocate,curator}_scenarios/

agent/performer/tests/unit/
├── test_advocate_report_path.py
├── test_curation_report_path.py
└── test_github_labels_and_project.py
```

**Structure Decision**: the existing single-project layout. Each role is its own
workflow package under `performer/workflows/`, siblings of `reviewer`,
`security` and `closer`, because they share the gate discipline and nothing
else. The daemon-side decision for both roles lives in one new service rather
than two, since the gate logic (enabled, not in flight, not exhausted, cooldown
elapsed) is identical and only the trigger condition differs.

## Phase sequencing

1. **Foundations**: the two terminal statuses, `Score.project_id`, the two new
   performer GitHub functions, the workflow registry entries and step budgets,
   the state fields and the schema bump, and the new configuration models.
   Nothing here is observable on its own, and everything else depends on it.
2. **User Story 1, the advocate**: the workflow package, its terminal branch in
   `main.py`, the daemon decision and dispatch, the poll and completion, then
   the deletion of the retired path in the same story so the two never coexist.
3. **User Story 2, the curator**: the workflow package, its terminal branch, its
   daemon pass, and the `check_board` filter narrowing.
4. **User Story 3**: the two personas and the three documents.
5. **Verification**: mutations, both suites, the adversarial review, an image
   rebuild, and live rounds for both roles recorded against SC-009 and SC-010.

## Risks this plan actively guards

- **The shared tail opens a pull request.** `handle_status`'s role cascade falls
  through to the implementer path that lints, pushes and opens a pull request,
  and its own comment states every role before now returns earlier. Each new
  role's branch is placed before that tail and its status is added to the closed
  `PerformerStatusType`, or the run hangs instead. Covered by a test per role
  asserting no push and no pull request, which is SC-003.
- **The stale in-flight marker.** The existing card-less paths document a
  deadlock from setting the marker after the dispatch. It is set before, rolled
  back on a synchronous raise, and deliberately not persisted.
- **The lost outcome.** A card-less completion moves no lifecycle signature and
  the snapshot save is gated on one, so each completion handler force-flushes
  through `snapshot_save_fn`, as the bootstrap completion already does.
- **Restart duplication.** Whether an issue was already handled is read from its
  labels and its presence on the board, never from memory, so a restart cannot
  produce a second reply or a second board entry.
- **The dropped synthetic id.** The transport reads `card_context["id"]`; nothing
  reads `card_id`. The wiki-init precedent sets `card_id`, so its synthetic id
  never reaches the wire. Both roles use `id`, and the analysis that found this
  is recorded rather than left as a comment on the precedent.

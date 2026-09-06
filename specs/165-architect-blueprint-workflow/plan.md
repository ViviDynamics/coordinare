# Implementation Plan: Architect Role Workflow with a Blueprint Hand-off

**Branch**: `165-architect-blueprint-workflow` | **Date**: 2026-09-06 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/165-architect-blueprint-workflow/spec.md`

## Summary

Add the second consumer of the spec-164 role-workflow layer: an architect workflow (intake, survey, blueprint, size, report) that reads a bounded slice of the repository through a code-enforced read-only allow-list, produces one schema-validated blueprint, and commits nothing. Coordinare persists the blueprint on the card's session, slices it into three briefs at dispatch (implementer, documenter, QA), decides the card's ceremony from its size, and runs the documenter as an out-of-lifecycle side run concurrently with implementation when the documentation brief is non-empty. The performer's push path gains fetch-and-rebase-before-push and loses its force fallback on divergence, which the side run requires.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only. pydantic 2.x (blueprint and brief models), the 164 layer (`workflows/`: `WorkflowAdapter`, `Toolkit`, `budget`, `schema_guard`), `persona_service`, `state_store` (`PersistedSession`), `dispatch_performer` and `monitor_performer` nodes, `env_cache.check_and_trigger` as the out-of-lifecycle dispatch precedent, `workspace.push_branch`. No new external dependencies.
**Storage**: single-host JSON snapshot via `state_store.py`; schema version 16 -> 17 adds `PersistedSession.blueprint` and `PersistedSession.documenting_side`, both defaulting to `None`. Older snapshots load unchanged.
**Testing**: pytest (`tests/unit`, `tests/contract`, `agent/performer/tests` run separately because both trees carry a `conftest.py`); worktree runs need `PYTHONPATH=src:agent/performer/src`.
**Target Platform**: the performer container (Debian, `coordinare-performer:extra`) for the workflow; the coordinare daemon for the carrier and side run.
**Project Type**: single repository, two packages (coordinare, performer).
**Performance Goals**: architect round under 20 minutes p90 on the live fleet (SC-001); survey 12 commands at 4000 characters each; blueprint call 8000 tokens with one doubled retry; read timeout 900 s.
**Constraints**: the architect writes nothing to the working tree; no reader receives another reader's slice; the prose path is byte-for-byte unchanged with the flag off; no force push after a non-fast-forward anywhere.
**Scale/Scope**: one new workflow package (about 8 modules), a persisted field, three payload fields, one persona edit, one push-path change, one out-of-lifecycle side run, one QA plan-step input, an eval with three fixtures.

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Code Quality First | No dead code: the pooling-style trap from 164 is avoided by wiring every module on day one (survey allow-list, size rule, side run) and deleting nothing speculative. No new dependencies. | PASS |
| II. Testing Discipline | Unit tests per step and per rule (allow-list refusals, size thresholds, slice disjointness, push rebase). Contract test for the three payload fields. Deterministic eval with a stubbed model in CI; live eval on demand only. Mutation checks on every rule test (standing rule). | PASS |
| III. UX Consistency | Operator surface is one key (`workflow: architect`) matching 164; briefs render into prompts through `_card_docs` the way `qa_findings` does; block reasons name the failing field. | PASS |
| IV. Performance by Design | Budgets stated in the spec's table, provisional, replaced by SC-001's measurement from live logs (the #264 `workflow.model_call` events). | PASS |
| V. Clarity Before Action | Every decision in research.md cites the code path or measurement it rests on. | PASS |

Post-design re-check (after data-model and contracts): no gate changed. The push-path change is the one item touching every role; it is justified because the current force fallback is unsafe even without this feature, and it is covered by its own tests.

## Project Structure

### Documentation (this feature)

```text
specs/165-architect-blueprint-workflow/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── blueprint.schema.json          # the blueprint as JSON Schema (also what the model is shown)
│   └── dispatch-payload-additions.md  # rows to add to specs/contracts/dispatch-payload.md
├── checklists/requirements.md
└── tasks.md                           # produced by /speckit.tasks
```

### Source Code (repository root)

```text
agent/performer/src/performer/
├── workflows/
│   ├── __init__.py                    # + "architect": (workflows.architect, "ArchitectWorkflow")
│   ├── architect/
│   │   ├── __init__.py                # ArchitectWorkflow: intake -> survey -> blueprint -> size -> report
│   │   ├── models.py                  # Blueprint, Milestone, DataModelChange, Interface, Criterion, DocTopic (bounded)
│   │   ├── personas.py                # SURVEY, BLUEPRINT step personas
│   │   ├── intake.py                  # assemble card, assessment, clarifications (no model call)
│   │   ├── survey.py                  # model proposes commands; allow-list decides; truncation; budget
│   │   ├── allowlist.py               # read-only command policy (pure functions)
│   │   ├── blueprint.py               # the schema-guarded model call
│   │   ├── size.py                    # size rule (pure function)
│   │   └── report.py                  # PerformerResponse.report shape + write-free executed check
│   └── qa/plan.py                     # + verification_brief as the criteria source
├── backends/_card_docs.py             # + implementation_brief / documentation_brief prompt sections
├── models.py                          # Score: + implementation_brief, documentation_brief, verification_brief, implementer_single_turn
├── main.py                            # architecting post-processing: skip plan/tasks commits when report.blueprint present
└── workspace.py                       # push_branch: fetch + rebase before push; force only on a missing remote branch;
                                       # documentation-tree guard for the documenter side run

src/coordinare/
├── state_store.py                     # schema 17: PersistedSession.blueprint, .documenting_side
├── graph/nodes/monitor_performer.py   # lift report.blueprint + size into the session
├── graph/nodes/dispatch_performer.py  # slice briefs into card_context; implementer_single_turn; skip documenter when brief empty
├── graph/nodes/check_board.py         # trigger + monitor the documenting side run (env-bootstrap precedent)
├── eval/architect_scenarios.py        # stubbed (CI) and --live eval runner over the three fixtures
├── services/documenting_side.py       # DocumentingSideRun lifecycle: dispatch once per blueprint hash, monitor, record
├── services/persona_service.py        # implementer: brief-aware per-turn procedure; single-turn variant; no-documentation rule
└── config.py                          # KNOWN_WORKFLOWS += "architect"

tests/
├── unit/workflows/architect/          # models, allowlist, survey, size, blueprint, end-to-end with stub model
├── unit/graph/nodes/                  # lift, slicing, single-turn, side-run trigger/monitor
├── unit/services/                     # documenting_side, persona variants, push rebase
├── contract/test_dispatch_payload.py  # + three brief fields and implementer_single_turn
└── eval/architect_scenarios/          # fixtures (3), generator, stub model, scoring, --live mode
```

**Structure Decision**: mirror 164 exactly. The workflow is a sibling package under `workflows/`; the carrier follows the `qa_findings` path with the one addition of persistence; the side run follows the env-bootstrap precedent rather than touching the linear lifecycle.

## Complexity Tracking

| Item | Why it is needed | Simpler alternative rejected because |
| --- | --- | --- |
| Out-of-lifecycle side run for the documenter | The lifecycle is one stage per session; a concurrent stage would change `_advance_stage`, slots and the monitor everywhere | Running the documenter after the implementer is today's behaviour; a second branch and PR doubles review load |
| Push-path change touching every role | Two performers on one branch with a force fallback would destroy commits | Scoping the rebase to the documenter leaves the unsafe fallback for the concurrent implementer |
| Persisted blueprint (schema bump) | The blueprint drives three later dispatches and must survive a restart | Graph-state-only (as `qa_findings`) loses it on restart mid-lifecycle |

## Phases

- **Phase 0 (research.md)**: complete; no NEEDS CLARIFICATION remained.
- **Phase 1 (design)**: data-model.md, contracts/, quickstart.md; agent context updated.
- **Phase 2 (tasks.md)**: produced by `/speckit.tasks`, organised by the four user stories with the push-path change and the persisted field in the foundational phase.

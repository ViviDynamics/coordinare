# Tasks: Role-workflow plugin layer, with QA as the first workflow

**Branch**: `164-qa-role-workflow` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**Note on user stories**: `spec.md` is organised by goals (G1-G4) and functional
requirements rather than prioritised user stories. The four stories below are
derived from those goals, in dependency order. Each is independently testable.

**Tests are REQUIRED** for this feature: Constitution Principle II is
non-negotiable, FR-015 to FR-018 name specific regression tests, and the project
follows TDD. Write the test, watch it fail, then implement.

**MVP** is US1 + US2. US3 makes QA's output useful to the next stage; US4 makes
"better" falsifiable. Neither is required for the layer to function.

---

## Phase 1: Setup

- [ ] T001 [P] Record PROVISIONAL performance budgets in `specs/164-qa-role-workflow/plan.md` from whatever QA-duration data is already available. Does NOT block implementation (analysis H2: gating Phase 1 on a live deployment blocks everything behind production data availability)
- [ ] T001b Measure current QA stage p50/p95 over at least 10 real cards on the next live run and replace the provisional budgets with measured ones. Must land before the feature is called done, not before code starts
- [x] T002 Back-fill the performance budgets from `plan.md` into a Success Criteria section in `specs/164-qa-role-workflow/spec.md` (Constitution Principle IV requires them in the spec)
- [x] T003 [P] Create the package skeleton `agent/performer/src/performer/workflows/` with `__init__.py`, and `tests/unit/workflows/` for its tests

---

## Phase 2: Foundational (blocks all user stories)

- [x] T004 Define `RoleWorkflow`, `WorkflowResult` and `WorkflowMetrics` in `agent/performer/src/performer/workflows/base.py` per `contracts/role_workflow.md` and `data-model.md`
- [x] T005 Create the workflow registry `SUPPORTED_WORKFLOWS` and `get_workflow()` in `agent/performer/src/performer/workflows/__init__.py`, mirroring `backends/__init__.py` including the module-level constant and `UnsupportedWorkflowError`
- [x] T006 [P] Write failing tests for the registry in `tests/unit/workflows/test_registry.py`: unknown name raises, known name resolves, the constant is importable without instantiating
- [x] T007 [P] Write failing tests for budget behaviour in `tests/unit/workflows/test_budget.py` covering FR-009 and FR-016: a `finish_reason: length` response retries once at double budget; a truncation surfaces as `TruncatedResponse` and never as a parse error; exceeding the 12-call ceiling raises `ModelCallCeilingExceeded`
- [x] T008 Implement `Budget`, the truncation retry and the call ceiling in `agent/performer/src/performer/workflows/budget.py` to make T007 pass
- [x] T009 [P] Write failing tests for schema enforcement in `tests/unit/workflows/test_schema_guard.py` covering FR-010 and FR-017: an out-of-schema response reprompts exactly once, then raises `SchemaViolation`
- [x] T010 Implement `schema_guard.py` in `agent/performer/src/performer/workflows/` to make T009 pass
- [x] T011 Implement the `Toolkit` in `agent/performer/src/performer/workflows/toolkit.py` with `run_command`, `capture_screenshot` (delegating to existing `qa_capture`), `dom_snapshot`, `call_model` (wrapping T008 and T010), `get_backend` and `emit`
- [x] T012 [P] Write failing tests in `tests/unit/workflows/test_toolkit.py` asserting the six toolkit guarantees in `contracts/role_workflow.md`, particularly that `run_command` returns a real exit code and `capture_screenshot` returns `None` rather than an unverified path

**Checkpoint**: the layer exists and is tested, but nothing uses it.

---

## Phase 3: US1 — A role can opt into a workflow, and opting out changes nothing (P1)

**Goal**: G1. The mechanism works end to end without changing the
coordinare/performer contract.

**Independent test**: configure a trivial no-op workflow for a role, dispatch,
and confirm a normal `PerformerResponse` comes back; remove the config line and
confirm byte-identical behaviour to today.

- [x] T013 [P] [US1] Add an optional `workflow: str | None` field to the role config model in `src/coordinare/models/` and validate it against the registry at config load, matching how `backend` is handled
- [x] T014 [P] [US1] Write failing tests in `tests/unit/test_config_workflow_field.py`: absent field defaults to `None`; an unknown workflow name is rejected at load, not at dispatch
- [x] T015a [US1] Declare `workflow` as an explicit field on `Score` in `agent/performer/src/performer/models.py`. MUST land before T015 — `Score` uses `extra="ignore"`, so an unregistered field is silently dropped and the workflow would appear configured while never running (analysis G1)
- [x] T015b [US1] Add a contract-test assertion for `workflow` in `tests/contract/test_dispatch_payload.py` per the contract's Change Protocol step 5
- [x] T015 [US1] Carry the workflow name through the job payload in `src/coordinare/services/http_performer_service.py` so the performer receives it at start
- [x] T016 [US1] In `agent/performer/src/performer/main.py`, branch the dispatch: when a workflow is configured, run it and build the `PerformerResponse` from its `WorkflowResult`; otherwise take today's path unchanged (FR-005)
- [x] T017 [US1] Write failing tests in `tests/unit/workflows/test_dispatch_branch.py` proving the no-workflow path is unchanged and the workflow path produces an equivalent response shape
- [x] T017b [US1] Enforce FR-004 in `agent/performer/src/performer/workflows/__init__.py`: resolve workflows ONLY from the trusted package directory, rejecting any name that resolves outside it
- [x] T017c [US1] Write failing tests in `tests/unit/workflows/test_trusted_path.py`: a workflow name resolving outside the package dir is refused; a path-traversal name is refused; a workflow file placed in a cloned repo is never loaded (FR-004, spec 144 trust boundary)
- [x] T018 [P] [US1] Implement a `noop` workflow in `agent/performer/src/performer/workflows/noop.py` that runs one backend and returns its report, used as the contract-preservation fixture
- [x] T018b [US1] Write a test in `tests/unit/workflows/test_no_callhome.py` asserting FR-001: no coordinare-bound call occurs during `workflow.run()`. This is the invariant the whole design rests on and nothing else verifies it
- [x] T019 [US1] Wire step transitions to `BackendEvent`s via `toolkit.emit` (FR-008) and assert in `tests/unit/workflows/test_events.py` that a run emits one event per step

**Checkpoint**: the layer is usable and provably inert when unconfigured.

---

## Phase 4: US2 — QA runs as six verifiable steps (P2)

**Goal**: G2. QA plans, baselines, executes, observes, judges and reports.

**Independent test**: run the QA workflow against the `healthy` and `regression`
scenario fixtures and confirm the correct verdict class for the correct reason.

- [x] T020 [P] [US2] Define `TestPlan`, `PlanCheck`, `FlowStep`, `ExecutedCheck`, `Observation` and `VisualDelta` as pydantic models in `agent/performer/src/performer/workflows/qa/models.py` per `data-model.md`
- [x] T021 [P] [US2] Write the six per-step personas in `agent/performer/src/performer/workflows/qa/personas.py`, each scoped to one step (FR-006)
- [ ] T022 **PARTIAL.** Model-dependent steps are deterministic and the truncation / out-of-schema / malformed cases are all covered as hand-written fixtures in the unit tests. What is NOT done is recording them from the actual design-session transcripts. The behaviours are pinned; the fixtures are synthetic, which is weaker evidence of what a real model does
- [x] T023 [US2] Implement the plan step in `agent/performer/src/performer/workflows/qa/plan.py`: emit a `TestPlan` bound to acceptance criteria, failing closed when it produces no checks (R7)
- [x] T024 [US2] Write failing tests in `tests/unit/workflows/qa/test_plan.py`: every check binds to a criterion; an empty plan fails closed rather than passing
- [x] T025 [US2] Implement the baseline step in `agent/performer/src/performer/workflows/qa/baseline.py` using `git worktree add` at the merge-base (R4), capturing DOM and screenshots for the plan's surfaces
- [x] T026 [US2] Write failing tests in `tests/unit/workflows/qa/test_baseline.py` asserting the baseline is SKIPPED when the plan has no visual or flow checks (a plan.md performance budget, verified by test not inspection) and that the head worktree is never destructively checked out
- [x] T027 [US2] Implement the execute step in `agent/performer/src/performer/workflows/qa/execute.py`: commands via `run_command`, flows via a coordinare-owned Playwright driver consuming declarative `FlowStep`s
- [x] T028 [US2] Write failing tests in `tests/unit/workflows/qa/test_execute.py`: an unknown `FlowStep.action` is a schema violation, not an improvised action; exit codes are real
- [x] T029 [US2] Implement the observe step in `agent/performer/src/performer/workflows/qa/observe.py`, pooling on `(kind, position)` with labels read from the DOM (R3)
- [x] T030 [US2] Write the FR-015 regression test in `tests/unit/workflows/qa/test_observe_pooling.py`: three runs naming one element `workspace`, `workspace / acme hq` and `acme hq` pool to ONE observation, not zero
- [x] T031 [US2] Implement the judge step in `agent/performer/src/performer/workflows/qa/judge.py`: per-criterion verdicts bound to `ExecutedCheck`s, plus the before/after delta with unexpected changes
- [x] T032 [US2] Write the FR-018 regression test in `tests/unit/workflows/qa/test_judge_delta.py`: an element present in before and absent in after yields `unexpected_regression`
- [x] T033 [US2] Write a test in `tests/unit/workflows/qa/test_judge_evidence.py` asserting a criterion with no bound `ExecutedCheck` can never be counted as passed
- [x] T034 [US2] Implement the report step in `agent/performer/src/performer/workflows/qa/report.py`, assembling the report and publishing evidence through the existing `cdn_upload` path
- [x] T035 [US2] Implement `QAWorkflow` in `agent/performer/src/performer/workflows/qa/__init__.py` sequencing the six steps, and register it as `qa`
- [x] T036 [US2] Verify the existing `qa_verdict` evidence floor still gates the workflow's output unchanged (FR-007) with a test in `tests/unit/test_qa_verdict_workflow_output.py`

**Checkpoint**: QA produces a verdict from verifiable steps. This plus US1 is the MVP.

---

## Phase 5: US3 — QA hands back a repair brief (P3)

**Goal**: G3. QA's output is actionable input for the next implementer round.

**Independent test**: fail a scenario, then confirm `qa_findings` reaches the
next dispatch's `card_context` with evidence attached.

- [x] T037 [P] [US3] Define the `Finding` model in `agent/performer/src/performer/workflows/qa/models.py` per `contracts/qa_findings.md`, shape-compatible with `scanner_findings`
- [x] T038 [US3] Emit `qa_findings` from the report step and place them on the `PerformerResponse` in `agent/performer/src/performer/main.py`
- [x] T038b [US3] Declare `qa_findings` on `Score` and add its contract-test assertion in `tests/contract/test_dispatch_payload.py` (analysis M2; contract Change Protocol steps 3 and 5)
- [x] T039 [US3] Carry findings into `card_context["qa_findings"]` in `src/coordinare/graph/nodes/dispatch_performer.py`, mirroring the `scanner_findings` lines at 1355-1357
- [x] T040 [P] [US3] Write failing tests in `tests/unit/test_qa_findings_carrier.py`: findings survive the round trip into the next dispatch context; the dedup key `(file, line, category)` behaves as it does for scanner findings
- [x] T041 [US3] Write the FR-013 test in `tests/unit/workflows/qa/test_finding_evidence.py`: a finding produced by an executed check MUST carry its evidence; one without evidence and without `environment_error` on the report is a contract violation
- [x] T042 [US3] Write the FR-014 test in `tests/unit/workflows/qa/test_finding_no_prescription.py` asserting no finding field carries a prescribed fix

---

## Phase 6: US4 — QA's behaviour is measurable (P4)

**Goal**: G4. "Better" becomes falsifiable.

**Independent test**: run the eval and get a pass rate per scenario.

- [x] T043 [P] [US4] Define `ScenarioFixture` and the manifest loader in `tests/eval/qa_scenarios/fixtures.py` per `data-model.md`
- [x] T044 [US4] Implement fixture repo generation from a YAML manifest into a temp directory in `tests/eval/qa_scenarios/generate.py`, creating base and head commits (R8 — never commit a `.git` directory)
- [x] T045 [P] [US4] Write the six scenario manifests in `tests/eval/qa_scenarios/fixtures/`: `healthy`, `regression`, `misplaced`, `incomplete`, `cosmetic_noop`, `env_broken`, each with a minimal Python HTTP stub app (FR-021)
- [x] T046 [US4] Implement the eval runner `src/coordinare/eval/qa_scenarios.py` with `--repeats`, reporting a pass rate per scenario and asserting qualitatively: verdict class correct, failure names the right artifact, healthy never fails (FR-019)
- [ ] T047 [US4] Emit `WorkflowMetrics` including rounds-to-green instrumentation so the spec's success metric is measurable rather than inferred
- [x] T048 [US4] Add a README to `tests/eval/qa_scenarios/` stating explicitly that this suite MUST NOT be wired into CI, and why (Constitution Principle II, see plan.md)

---

## Phase 7: Polish and cross-cutting

- [x] T049 **Done as EXTRACT, not delete.** The 498-line QA block in `main.py` was load-bearing for the spec-164 workflow path too (the adapter surfaces the report as `backend_status.output`, which that block consumes to commit tests, upload evidence and post the PR comment), so deleting it was never an option. It now lives in `agent/performer/src/performer/qa_postprocess.py` as `finalize_qa(perf, backend_status, settings)` with its 13 QA-only helpers beside it, body verbatim; `main.py` delegates in one line and shrank 4,326 → 3,332 lines. Collaborators are looked up on `performer.main` at call time so the ~85 existing `patch("performer.main.<name>")` sites keep working — pinned by a mutation-verified test. Behavioural net: the 44 `role="qa"` `handle_status` tests, unchanged
- [ ] T050 [P] Run mutation checks on the evidence floor and the pooling threshold: break each rule once in the real tree and confirm a test fails (a MISSED result usually means the mutation never applied)
- [x] T051 [P] Update `docs/onboarding/` to describe the workflow layer alongside harnesses and shims
- [ ] T052 **Cannot be committed — `config.yaml` is gitignored.** The stale CAVEAT (around line 415) claiming the container has no headless browser now argues against shipped code, but the file is local-only, so this is an operator edit rather than a repo change
- [ ] T053 Run `.venv/bin/ruff check` and the full `.venv/bin/pytest` suite; confirm coverage has not decreased (Constitution Quality Gates)

---

## Dependencies

```text
Setup (T001-T003)
  └─> Foundational (T004-T012)      ← blocks everything
        └─> US1 (T013-T019)          ← blocks US2 (needs the dispatch branch)
              └─> US2 (T020-T036)    ← MVP complete here
                    ├─> US3 (T037-T042)   ← independent of US4
                    └─> US4 (T043-T048)   ← needs a working QA workflow to score
                          └─> Polish (T049-T053)
```

T049 depends on US2 being complete and verified; deleting the old block before
the new steps are proven would remove the fallback.

## Parallel opportunities

- **Foundational**: T006, T007, T009, T012 (all separate test files)
- **US1**: T013 and T014 (coordinare side) run parallel to T018 (performer side)
- **US2**: T020, T021, T022 are independent groundwork before any step lands
- **US4**: T043 and T045 (fixtures) parallel to T046 (runner)
- **Polish**: T050, T051 independent

## Independent test criteria

| Story | Passes when |
|---|---|
| US1 | A no-op workflow returns a valid response; removing the config line reproduces today's behaviour exactly |
| US2 | `healthy` yields pass, `regression` yields fail naming the broken thing |
| US3 | A failed run's `qa_findings` reach the next dispatch with evidence attached |
| US4 | The eval reports a pass rate per scenario across N repeats |

## Recorded follow-ups (second review round, 2026-09-05)

Deferred deliberately, with the reason, rather than rushed at the end of a long
session. Both are performance items and Constitution Principle IV says measure
before optimising; T001b has not measured yet.

- [ ] T054 One browser per QA run. `read_dom` launches a full Chromium per call,
      so N planned surfaces cost 2N launches (base + head). Cheap on a laptop,
      reportedly 40s each on a loaded host. Fix is a per-run DOM session the
      toolkit reuses; needs T001b's numbers to size the win.
- [ ] T055 Boot head and base apps concurrently. They are independent processes
      on different ports and the worktree checkout does not depend on the head
      boot; `asyncio.gather` halves visual-card boot time. Same measurement
      dependency.
- [ ] T056 Model-description pass for layout defects (advisory). The pooling
      path built for it was never wired in and was removed as dead code in the
      second review round; DOM reads carry the comparison. Re-add only as a
      wired, tested, advisory step -- never as a hook waiting for later.


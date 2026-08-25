# Tasks: Board-Simulation Benchmark — Phase 4 (automated config optimizer)

**Input**: Design documents from `/specs/137-board-bench-optimizer/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md
**Tests**: included — constitution II mandates them; SC-001/002 (convergence,
determinism) are only provable as tests against synthetic objectives (TDD:
search-logic tests first).

## Phase 1: Setup

- [X] T001 Verify baseline: `make lint` and `make test` green on the branch before changes

## Phase 2: User Story 1 — budgeted adaptive search (P1)

**Goal**: `bench/optimizer.py` — seeded evolutionary local search (best-first
single-dimension mutation + random restarts), fingerprint memoization, hard
evaluation/wall-clock budget, injectable evaluator.

**Independent Test**: against synthetic planted-optimum evaluators: finds the
optimum within ≤ half the space (≥3 seeds), never exceeds the budget, never
re-evaluates a fingerprint, identical seed ⇒ identical trace.

- [X] T002 [P] [US1] Search-logic unit tests (fail first): planted-optimum convergence over the shipped default space with ≥3 seeds and budget ≤ half the space (SC-001); budget-N cap with stop_reason=budget_evaluations; wall-clock cap (fake clock) with stop_reason=budget_wall_clock and in-flight completion note; memoization (duplicate proposal = cache hit, uncharged); seed determinism (two runs byte-identical excl. timestamps, SC-002); full-space exhaustion stop; tie → earliest-evaluated + tie_note; all-failed ⇒ recommendation None in `tests/unit/test_137_optimizer.py`
- [X] T003 [US1] Implement `src/coordinare/bench/optimizer.py` part 1: `OPTIMIZER_SCHEMA_VERSION=1`, `EvaluationRecord`/`ComparisonRow`/`Recommendation`/`BudgetSpent`/`OptimizerArtifact` (write-time FR-010 invariants in `to_validated_json()`, rendered `optimizer-report.md`), the `Evaluator` protocol, and `run_optimizer(loaded, evaluate, *, seed, max_evaluations, max_seconds, …)` per research R1/R4/R5 (baseline + candidates first, best-first mutation, seeded restarts, memo table, stop reasons)

**Checkpoint**: SC-001/SC-002 pass on synthetic objectives.

## Phase 3: User Story 2 — noise-aware evaluation + real path (P1)

**Goal**: the shipped `real_evaluator` (materialize → `run_board(config=…)` →
`score_run` × repeats) and the enforced real-search precondition.

- [X] T004 [P] [US2] Unit tests (fail first): failed evaluation recorded + search continues (FR-007, synthetic evaluator raising); repeats + repeats_source recorded in the artifact; artifact round-trip + invariant violations refuse to write in `tests/unit/test_137_optimizer.py`
- [X] T005 [US2] Implement `real_evaluator(...)` in `src/coordinare/bench/optimizer.py` (per-repeat run+score dirs `<session>/<eval-id>/<repeat>/`, score.json written, mean scalar/components, failure captured — mirrors `sweep._run_point` semantics; research R2)
- [X] T006 [US2] CLI `scripts/board_optimize.py` (research R7): `--space --run-dir --seed --max-evaluations --max-seconds --repeats|--noise-report [--resolution --max-repeats] --real --fixtures` + shared judge/weight flags; `--real` without `--noise-report` exits 2 naming the 135 prerequisite + the producing command (FR-006/SC-004); prints space size + budget before, recommendation + accounting after; unique session dir (same-second guard, 135/136 pattern)

## Phase 4: User Story 3 — trace/accounting/report + integration (P2)

- [X] T007 [US3] Integration test in `tests/integration/test_137_optimizer_end_to_end.py`: (a) stub search over a small real space (tiny fixture, budget ~6) → optimizer.json + report exist, accounting reconciles (trace = charged + cached ≤ budget; coverage matches distinct fingerprints), baseline + candidates in the comparison, stub caveat in notes (SC-003); (b) CLI real-mode refusal without noise report (subprocess or main() call, exit 2 + message names 135); (c) recommendation overrides re-materialize to the recorded fingerprint
- [X] T008 [US3] Run the real deliverable searches and commit `specs/137-board-bench-optimizer/optimizer-findings.md` (SC-005): synthetic convergence evidence (from the unit suite, ≥3 seeds), a real stub-substrate end-to-end search over the default space (honestly labeled signal-free, "tie within noise" expected), budget accounting, and the standing instruction that real recommendations require the real-substrate noise measurement first

## Phase 5: Polish & Cross-Cutting

- [X] T009 Run `make fmt`, `make lint`, `make test-all`; fix findings

## Dependencies

- T002 → T003 (TDD); T004 alongside T002 after the module skeleton exists conceptually (same file, write tests first).
- Phase 3 needs Phase 2's loop; Phase 4 needs Phases 2-3. T008 needs T006.
- Parallel: T002 ∥ T004 (one test file, one author — sequential in practice).

## Implementation Strategy

MVP = Phases 1-2 (the proven search loop, SC-001/002). Phases 3-4 wire the
real pipeline, the enforced gate, and the honest deliverables (SC-003/004/005).
Single PR — one coherent deliverable per issue #185.

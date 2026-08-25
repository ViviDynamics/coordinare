# Implementation Plan: Board-Simulation Benchmark — Phase 4 (automated config optimizer)

**Branch**: `137-board-bench-optimizer` | **Date**: 2026-08-24 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/137-board-bench-optimizer/spec.md` (issue #185)

## Summary

Close the benchmark program's loop: a **seed-deterministic evolutionary local
search** (best-first single-dimension mutation + random restarts, fingerprint
memoization — no config evaluated twice) over the 136 search space, evaluating
each proposal through the existing pipeline (materialize → `run_board(config=…)`
→ `score_run` × repeats) behind an **injectable evaluator** so convergence is
proven against a synthetic planted-optimum objective. Hard budget (evaluation
count + wall-clock), noise handling per 135 (repeats derived from a noise
report; a `--real` search **refuses to start** without one), and a versioned
artifact + report with the full trace, budget accounting, coverage, and a
baseline/candidates head-to-head.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv).
**Primary Dependencies (reused, no new runtime deps)**: pydantic 2.x
(artifact models, 134/135/136 conventions), the 136 space surface
(`bench/space.py`: `LoadedSpace`, `materialize`, `config_fingerprint`,
`resolve_path`), the 134 substrate seam (`bench/runner.run_board(config=…)`),
the 135 scorer (`bench/grader.score_run`, `bench/score.Weights`), the 136
repeats derivation (`bench/sweep.derive_repeats`) and noise report model
(`bench/noise.NoiseReport`), stdlib `random.Random(seed)` + `time.monotonic`,
structlog. **No new external dependencies** (research R1 justifies rejecting
BO libraries).
**Storage**: none new. Optimizer session dirs
`runs/<ts>-optimize/<eval-id>/<repeat>/` (usual `run.json` + `score.json`),
with `optimizer.json` + `optimizer-report.md` at the session root.
**Testing**: pytest via `make test`; `tests/unit/test_137_optimizer.py`
(search logic against synthetic evaluators — convergence, budget, memoization,
determinism, tie-break, failure isolation, accounting invariants, artifact
round-trip) + `tests/integration/test_137_optimizer_end_to_end.py` (stub
search over a small real space; real-mode refusal without a noise report).
All offline/deterministic.
**Target Platform**: Linux (dev + CI), same as coordinare.
**Project Type**: single project (coordinare).
**Performance / termination**: bounded by min(max_evaluations, wall-clock cap,
full-space coverage) × repeats × the 134 per-run budget; an in-flight
evaluation completes, then the loop stops (FR-002).
**Scale/Scope**: the shipped default space ≈ 2⁷ × 2 × 2 × 2 = low hundreds of
distinct combinations; budgets of 20–100 evaluations.

## Constitution Check

- **I. Code Quality First** — PASS. One new single-responsibility module
  (`bench/optimizer.py`) + one CLI; no new dependencies (the method choice is
  explicitly justified against adding BO libraries, research R1). Typed
  public surfaces; the evaluator is a small Protocol.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. The search algorithm is
  unit-tested against injected synthetic objectives (planted optimum, ≥3
  seeds, budget/memoization/determinism invariants — SC-001/002), failure
  isolation and accounting invariants tested directly; one integration test
  drives the real (stub) evaluator end-to-end. Deterministic, zero model cost.
- **III. User Experience Consistency** — N/A UI; `scripts/board_optimize.py`
  mirrors `board_sweep.py` conventions; the real-mode refusal error names the
  135 prerequisite and the command that produces it (FR-006/SC-004).
- **IV. Performance by Design** — PASS. Hard evaluation + wall-clock budget
  is the feature's own core requirement (FR-002); termination additionally
  guaranteed by full-space exhaustion.
- **V. Clarity Before Action** — PASS. Five material forks decided and
  recorded in spec Clarifications (gate satisfaction, synthetic validation,
  method choice, enforced real-search precondition, single objective). Zero
  `NEEDS CLARIFICATION` remain.

**Result**: All gates pass. No Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/137-board-bench-optimizer/
├── plan.md              # This file
├── spec.md              # Feature spec
├── research.md          # Phase 0 — decisions + rationale
├── data-model.md        # Phase 1 — optimizer entities
├── quickstart.md        # Phase 1 — how to run a search
├── contracts/
│   └── optimizer-artifact.md  # the result schema + guarantees
├── checklists/requirements.md
├── optimizer-findings.md      # SC-005 — committed findings from real runs
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/bench/
└── optimizer.py         # NEW — OPTIMIZER_SCHEMA_VERSION=1; EvaluationRecord/
                         #   Recommendation/BudgetSpent/OptimizerArtifact (+ render);
                         #   Evaluator protocol; real_evaluator() wrapping
                         #   materialize→run_board(config)→score_run×repeats;
                         #   run_optimizer(): seeded best-first mutation search with
                         #   random restarts, fingerprint memoization, hard budget
scripts/
└── board_optimize.py    # NEW — CLI mirroring board_sweep.py flags
                         #   (--space --seed --max-evaluations --max-seconds
                         #   --noise-report/--repeats, judge/weights, --real
                         #   refusing without --noise-report)

tests/
├── unit/test_137_optimizer.py             # search logic vs synthetic objectives + artifact
└── integration/test_137_optimizer_end_to_end.py  # stub end-to-end + real-mode refusal
```

**Structure Decision**: everything inside the self-contained
`src/coordinare/bench/` package; zero production-file touches this time (the
seam already exists from 136). Reuse over reimplementation: space
loading/materialization/fingerprints, the run seam, the scorer, and the
repeats derivation are all consumed as-is.

## Complexity Tracking

No constitution violations — section intentionally empty.

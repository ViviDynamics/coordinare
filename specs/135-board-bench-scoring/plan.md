# Implementation Plan: Board-Simulation Benchmark — Phase 2 (scoring + noise characterization)

**Branch**: `135-board-bench-scoring` | **Date**: 2026-08-23 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/135-board-bench-scoring/spec.md` (issue #183)

## Summary

Turn a spec-134 run artifact into a documented, reproducible **score object** —
per-card correctness graded against the fixture's planted ground truth with the
spec-077 deterministic + LLM-judge agree-to-PASS model lifted to whole-lifecycle,
plus cost and time components and a weighted scalar objective — and add a **repeat
runner** that runs one fixed config N times and emits a **noise report**
(per-component mean/variance, per-card verdict agreement). A committed written
go/no-go finding for Phase 4 (spec 137) is produced from a real measurement, with
the substrate-fidelity caveat (134's real-performer path is deferred) stated.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv).
**Primary Dependencies (reused, no new runtime deps)**: pydantic 2.x (score/noise
models mirroring `bench/artifact.py` conventions), the spec-134 bench package
(`src/coordinare/bench/` — `artifact.py`, `fixtures.py`, `runner.run_board`),
httpx (LLM judge over the LiteLLM proxy, same pathway `scripts/persona_bench.py`
uses), stdlib `statistics` (mean/variance), structlog. No new external
dependencies.
**Storage**: none new. `score.json` is written next to the `run.json` it scores;
the noise report is written into the repeat-session directory. No coordinare state.
**Testing**: pytest via `make test`; new tests `tests/unit/test_135_*.py` +
`tests/integration/test_135_score_end_to_end.py` (stubbed performer,
deterministic, zero model cost — CI never needs a live judge).
**Target Platform**: Linux (dev + CI), same as coordinare.
**Project Type**: single project (coordinare).
**Performance / termination**: scoring one artifact is pure local computation
(sub-second, judge calls aside); the repeat runner is bounded by N × the 134
per-run budget (`max_cycles` + wall-clock guard). Judge calls carry the same
timeout persona_bench uses.
**Scale/Scope**: one config, N ≤ ~10 repeats, a handful of cards per run.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. No new dependencies. New single-responsibility
  modules inside the existing `src/coordinare/bench/` package (`score.py`,
  `grader.py`, `judge.py`, `noise.py`); never imported by production paths.
  Typed public surfaces; weights are plain dataclass-style pydantic config, no
  speculative hierarchy.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. Unit tests per module
  (schema round-trip, deterministic grading truth table, agree-to-PASS gating,
  noise aggregation incl. failed repeats), one deterministic integration test
  (stub run → score → assert verdict). Judge is stubbed in tests; deterministic
  paths fully offline. Existing suite stays green (no 134 behavior change beyond
  a backward-compatible fixture field).
- **III. User Experience Consistency** — N/A UI; the CLI mirrors
  `board_bench.py`'s argparse conventions and fails loudly with actionable errors
  (unknown schema version, missing fixture, empty ground truth).
- **IV. Performance by Design** — PASS. Scoring is local + bounded; the repeat
  runner's budget is N × the existing per-run termination budget. No CI benchmark
  needed (harness tooling, not a production path).
- **V. Clarity Before Action** — PASS. The three material forks (scalar formula,
  stub-substrate noise measurement vs blocking on the 134 follow-up, judge
  optionality in CI) were resolved and recorded in the spec's Clarifications
  section. Zero `NEEDS CLARIFICATION` remain.

**Result**: All gates pass. No Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/135-board-bench-scoring/
├── plan.md              # This file
├── spec.md              # Feature spec
├── research.md          # Phase 0 — decisions + rationale
├── data-model.md        # Phase 1 — score/noise entities
├── quickstart.md        # Phase 1 — how to score + measure noise
├── contracts/
│   ├── score-object.md  # the score schema 136/137 consume
│   └── noise-report.md  # the aggregate schema + go/no-go inputs
├── checklists/requirements.md
├── noise-findings.md    # FR-009 — the committed go/no-go finding (written from a real run)
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/bench/
├── fixtures.py          # EXTEND — Fixture.expected_final_state ("merged" default | "blocked"),
│                        #   backward-compatible; empty ground_truth rejected at grading time
├── score.py             # NEW — ScoreObject + CardVerdict + ComponentVector + Weights models;
│                        #   schema_version, round-trip validate, write score.json next to run.json
├── grader.py            # NEW — deterministic whole-lifecycle checks + 077 categorize lift
│                        #   (PASS / FAIL_MODEL / FAIL_HARNESS) + agree-to-PASS composition
├── judge.py             # NEW — LLM judge over the LiteLLM proxy; same prompt/JSON contract as
│                        #   persona_bench.judge (077), whole-lifecycle evidence blob
└── noise.py             # NEW — run_repeats(): N × (run_board + score) → NoiseReport
                         #   (per-component mean/variance, per-card agreement, effective N)
scripts/
└── board_score.py       # NEW — CLI: `score` (grade an existing run dir) and
                         #   `noise` (repeat-run one config N times + aggregate)

tests/
├── unit/test_135_score_schema.py    # round-trip, weights embedded, deterministic reproducibility
├── unit/test_135_grader.py          # grading truth table + agree-to-PASS + judge-error fallback
├── unit/test_135_noise.py           # aggregation math, failed-repeat handling, effective N
└── integration/test_135_score_end_to_end.py  # stub run → score → correct verdict (free, deterministic)
```

**Structure Decision**: everything stays inside the self-contained
`src/coordinare/bench/` package 134 created — no parallel stack. `judge.py`
reimplements 077's judge contract inside the package rather than importing
`scripts/persona_bench.py` (scripts are not an importable package; refactoring the
077 harness is out of scope per PR-scope discipline — the *contract* is what's
reused: same prompt shape, same JSON verdict, same agree-to-PASS composition,
documented in research.md R3).

## Complexity Tracking

No constitution violations — section intentionally empty.

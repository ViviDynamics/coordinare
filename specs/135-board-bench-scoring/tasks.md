# Tasks: Board-Simulation Benchmark — Phase 2 (scoring + noise characterization)

**Input**: Design documents from `/specs/135-board-bench-scoring/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md
**Tests**: included — constitution II mandates them, and the grading truth table is
the executable acceptance of FR-003/FR-004 (TDD where natural: schema + grader
tests first, then implementation).

## Phase 1: Setup

- [X] T001 Verify baseline: `make lint` and `make test` green on the branch before changes (record any pre-existing failures)

## Phase 2: Foundational (blocking prerequisites for both stories)

- [X] T002 Extend `Fixture` with `expected_final_state: Literal["merged","blocked"] = "merged"` in `src/coordinare/bench/fixtures.py` (backward-compatible; manifest loader unchanged — research R2)
- [X] T003 [P] Add score models (`SCORE_SCHEMA_VERSION=1`, `Weights`, `CardVerdict`, `ComponentVector`, `ScoreObject` with `to_validated_json()`/`write()`/`load()` mirroring `artifact.py`) in `src/coordinare/bench/score.py` (data-model §A, contract score-object.md)
- [X] T004 [P] Unit tests for score schema: round-trip validation, weights embedded, unknown-artifact-version rejection, write-next-to-run.json in `tests/unit/test_135_score_schema.py`

**Checkpoint**: score object exists and round-trips — both user stories can now build.

## Phase 3: User Story 1 + 2 — score a run against ground truth with 077 agree-to-PASS (P1)

**Goal**: `board_score.py score` grades a 134 run artifact against planted ground
truth: deterministic whole-lifecycle checks + optional LLM judge, agree-to-PASS,
FAIL_MODEL/FAIL_HARNESS split, reproducible score object. (US1 and US2 share one
phase: the grader is one unit — US2 is the grading model US1's scoring engine uses.)

**Independent Test**: feed stored artifacts (built in-test) covering
correct-merge / wrong-merge / should-not-merge-but-merged / harness-error; verify
per-card verdicts and the component vector match hand-graded expectations; verify
judge-disagree blocks PASS and judge-off is labeled deterministic-only.

- [X] T005 [P] [US1] Grader truth-table unit tests (fail first): expected-merge + CI-green ⇒ PASS; merged-but-CI-red ⇒ FAIL_MODEL; expected-blocked but merged ⇒ FAIL_MODEL; expected-blocked and held ⇒ PASS; final_state=error / zero dispatches ⇒ FAIL_HARNESS; empty ground_truth ⇒ loud error; head_sha CI matching with fallback-to-last in `tests/unit/test_135_grader.py`
- [X] T006 [P] [US2] Agree-to-PASS unit tests (fail first): deterministic-pass + judge-disagree ⇒ FAIL_MODEL with both verdicts recorded; judge disabled ⇒ deterministic-only label; judge error/malformed ⇒ per-card fallback, never flips verdict; same-artifact-twice (judge off) ⇒ identical content excluding scored_at in `tests/unit/test_135_grader.py`
- [X] T007 [US2] Implement the LLM judge (077 contract: rubric + deterministic detail + evidence blob → strict JSON `{correct, quality, reason}`, temperature 0, httpx to LiteLLM `chat/completions`, error → `(None, None, "judge_error: …")`) in `src/coordinare/bench/judge.py` (research R3)
- [X] T008 [US1] Implement the whole-lifecycle grader: deterministic signals (R1), 077 categorize lift (PASS/FAIL_MODEL/FAIL_HARNESS/ERROR), agree-to-PASS composition, per-run `score_run(artifact, fixtures, weights, judge=None) -> ScoreObject` incl. component vector + scalar (R4) in `src/coordinare/bench/grader.py`
- [X] T009 [US1] CLI `score` subcommand (`--run-dir --fixtures --judge-* --w-* --cost-budget --time-budget`, loud errors on unknown schema / missing fixture / empty ground truth) in `scripts/board_score.py`
- [X] T010 [US1] Integration test: `run_board` (stub, tiny fixture) → `score_run` deterministic-only ⇒ PASS verdict, correctness_rate 1.0, score.json written + reloadable in `tests/integration/test_135_score_end_to_end.py`

**Checkpoint**: SC-001/SC-002 satisfiable — the scoring engine stands alone.

## Phase 4: User Story 3 — noise characterization across N repeats (P2)

**Goal**: `board_score.py noise` runs one fixed config N times, scores each repeat,
and emits the aggregate noise report; a committed written go/no-go finding for
Phase 4 (spec 137) is produced from a real N=5 measurement.

**Independent Test**: repeat runner with small N over the tiny fixture produces N
run+score pairs and one report with per-component mean/variance, per-card
agreement, and failed repeats reported with effective N.

- [X] T011 [P] [US3] Noise unit tests (fail first): sample variance (n−1) + `single_sample` flag; `None`-component handling (n = present values); failed repeat listed + excluded with effective N; config-fingerprint mismatch aborts; modal-verdict card agreement in `tests/unit/test_135_noise.py`
- [X] T012 [US3] Implement `run_repeats(fixtures, repeats, …) -> NoiseReport` + `NoiseReport`/`ComponentStats`/`RepeatFailure` models (`NOISE_SCHEMA_VERSION=1`, round-trip write of `noise-report.json` + rendered `noise-report.md`) in `src/coordinare/bench/noise.py` (data-model §C, contract noise-report.md)
- [X] T013 [US3] CLI `noise` subcommand (`--repeats --run-dir --fixtures --config --real` + judge/weight flags; session dir `runs/<ts>-noise/<i>/`) in `scripts/board_score.py`
- [X] T014 [US3] Integration: extend `tests/integration/test_135_score_end_to_end.py` with a 2-repeat stub noise run asserting report shape, effective N, and agreement=1.0
- [X] T015 [US3] Run the real measurement (stub substrate, N=5, deterministic-only + note on judge availability) and commit the written go/no-go finding with evidence + the substrate-fidelity caveat in `specs/135-board-bench-scoring/noise-findings.md` (FR-009, SC-004)

## Phase 5: Polish & Cross-Cutting

- [X] T016 [P] Verify FR-010 backward compatibility: existing 134 tests green, manifest without `expected_final_state` loads with default (assert in `tests/unit/test_135_grader.py` or `tests/unit/test_134_fixtures.py`)
- [X] T017 Run `make fmt`, `make lint`, `make test-all`; fix findings

## Dependencies

- T002–T004 block everything story-scoped. T005/T006 (tests) before T007/T008 (TDD).
- Phase 4 depends on Phase 3 (noise scores runs via the grader). T015 depends on T012–T013.
- Parallel: T003∥T004 after T002; T005∥T006∥T011 anytime after T003.

## Implementation Strategy

MVP = Phases 1–3 (the standalone capability benchmark, SC-001/002/005).
Phase 4 delivers the go/no-go gate (SC-003/004). Single PR — the phases are one
coherent deliverable per issue #183.

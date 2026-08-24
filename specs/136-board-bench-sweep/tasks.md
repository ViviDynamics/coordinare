# Tasks: Board-Simulation Benchmark — Phase 3 (config search-space + sweep runner)

**Input**: Design documents from `/specs/136-board-bench-sweep/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md
**Tests**: included — constitution II mandates them; the space-validation truth
table and coverage accounting are the executable acceptance of FR-002/SC-004
(TDD where natural: tests first for space + sweep aggregation).

## Phase 1: Setup

- [X] T001 Verify baseline: `make lint` and `make test` green on the branch before changes

## Phase 2: Foundational (blocking prerequisites for all stories)

- [X] T002 Add the config-injection seam: `run_board(..., config: CoordinareConfiguration | None = None)` sets `daemon.state["config"]` (global) + `daemon.state["symphony_configs"]` (by name) and, when injected, records the materialized config's SHA-256(16-hex) fingerprint with `source_path="<materialized>"` in `src/coordinare/bench/runner.py` (research R1; default None = unchanged behavior)
- [X] T003 [P] Regression: existing 134/135 tests green with no config passed; new unit test that an injected config lands in daemon state + fingerprint reflects materialized content in `tests/unit/test_136_sweep.py` (seam section)

**Checkpoint**: materialized configs can reach a run.

## Phase 3: User Story 1 — declarative, validated search space (P1)

**Goal**: `bench/space.py` — SearchSpace/Dimension/Candidate models, YAML
loader, load-time validation, dotted-path materializer with
symphonies-by-name addressing, fingerprints.

**Independent Test**: load valid/invalid definitions; verify unknown paths,
out-of-schema values, missing baseline all fail loudly naming the element;
loaded space enumerates exactly the declared points.

- [X] T004 [P] [US1] Space unit tests (fail first): valid space loads with exact dimensions/candidates; unknown dimension path fails naming it; schema-rejected choice fails naming dimension+value; missing/invalid baseline refuses; empty definition (no dims, no candidates) fails; candidates-only definition valid; symphonies-by-name path resolves; distinct points ⇒ distinct fingerprints; identical materializations detected in `tests/unit/test_136_space.py`
- [X] T005 [US1] Implement `src/coordinare/bench/space.py`: `SpaceError`, `Dimension`/`Candidate`/`SearchSpace` models, `load_space(path)` (YAML, baseline resolved relative to the space file, full FR-002 validation incl. materializing every choice/candidate once), `materialize(baseline_dump, overrides)`, `config_fingerprint(config)` (research R2, data-model §A)
- [X] T006 [P] [US1] Commit the shipped default space + minimal baseline: `benchmarks/spaces/baseline.yaml` (one `bench` symphony with persona_scope present; performers configured) and `benchmarks/spaces/default.yaml` (gate dimensions 075/089/090×3/095, `max_concurrent_cards`, implementer/reviewer mode choices, 4 candidates; known-not-sweepable documented) per research R6 + contract search-space.md; unit test loads it clean in `tests/unit/test_136_space.py`

**Checkpoint**: SC-001's space half satisfiable — a validated default space loads.

## Phase 4: User Story 2 — ablation runner (P1)

**Goal**: `run_sweep(mode="ablation")` runs baseline + one run per
(dimension, non-baseline value) via 134+135, computes per-dimension deltas,
survives point failures.

**Independent Test**: stub ablation over a small space → N+1 runs, distinct
fingerprints per point, delta table vs baseline, failed point recorded not
fatal.

- [X] T007 [P] [US2] Sweep unit tests (fail first): ablation enumeration (baseline + Σ non-baseline choices; baseline-coinciding choice skipped with coverage note); deltas = point minus baseline (scalar + components); failed point → dropped + sweep continues (FR-009); repeats math from a noise report (`ceil((stdev/resolution)²)` clamped, fallback when stats unusable); coverage invariant `declared == scored + dropped`; artifact round-trips + renders markdown in `tests/unit/test_136_sweep.py`
- [X] T008 [US2] Implement `src/coordinare/bench/sweep.py`: `SWEEP_SCHEMA_VERSION=1`, `PointResult`/`AblationDelta`/`DroppedPoint`/`Coverage`/`SweepArtifact` (round-trip write of `sweep.json` + rendered `sweep-report.md` incl. stub-fidelity caveat), `derive_repeats(noise_report, resolution, max_repeats)`, `run_sweep(space, mode, session_dir, repeats…, weights, judge, stub)` running points sequentially via `run_board(config=…)` + `score_run` per repeat (research R4/R5, data-model §B)
- [X] T009 [US2] CLI `ablate` subcommand in `scripts/board_sweep.py` (`--space --run-dir --repeats|--noise-report --resolution --max-repeats --real` + board_score.py judge/weight flag set; prints enumerated point count before running, coverage summary after; loud errors name the offending element)

**Checkpoint**: SC-002 satisfiable — ablation end-to-end in stub mode.

## Phase 5: User Story 3 — curated candidate ranking (P2)

- [X] T010 [P] [US3] Candidate unit tests (fail first): K candidates → ranking by mean scalar with unrankable (None) last + marked; duplicate materialized fingerprints flagged via `duplicate_of` in `tests/unit/test_136_sweep.py`
- [X] T011 [US3] Implement candidate mode in `run_sweep` + the `candidates` CLI subcommand in `scripts/board_sweep.py` (shares all US2 plumbing)

## Phase 6: User Story 4 — honest reporting + integration (P2)

- [X] T012 [US4] Integration test in `tests/integration/test_136_sweep_end_to_end.py`: (a) stub ablation over a 2-dimension space (incl. `max_concurrent_cards`) → sweep.json + report exist, coverage reconciles, per-point fingerprints distinct; (b) candidates run ranks 2 candidates; (c) SC-006 behavioral proof: `assignee_filter` set vs unset flips the same fixture between merged and never-dispatched (research R3); (d) forced point failure appears in dropped with its error and in the rendered summary
- [X] T013 [US4] Run a real stub-mode ablation over the shipped default space (`board_sweep.py ablate --space benchmarks/spaces/default.yaml`) and commit the observed report as `specs/136-board-bench-sweep/ablation-findings.md` with the stub-fidelity caveat and the coverage reconciliation (the issue's "ablation results" deliverable at current substrate fidelity)

## Phase 7: Polish & Cross-Cutting

- [X] T014 Run `make fmt`, `make lint`, `make test-all`; fix findings

## Dependencies

- T002 blocks all sweep execution (T008+). T004→T005 and T007→T008 (TDD).
- Phase 5 needs Phase 4's runner; Phase 6 needs Phases 3-5. T013 needs T006+T009.
- Parallel: T003∥T004 after T002; T006 after T005; T007 anytime after T005.

## Implementation Strategy

MVP = Phases 1-4 (validated space + ablation, SC-001/002/006). Phases 5-6 add
ranking + the honesty deliverables (SC-003/004). Single PR — one coherent
deliverable per issue #184.

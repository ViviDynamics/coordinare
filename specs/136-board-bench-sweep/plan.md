# Implementation Plan: Board-Simulation Benchmark — Phase 3 (config search-space + sweep runner)

**Branch**: `136-board-bench-sweep` | **Date**: 2026-08-23 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/136-board-bench-sweep/spec.md` (issue #184)

## Summary

Make coordinare configuration a sweepable object: a **declarative, validated
search space** (named baseline + swept dimensions + named candidates, everything
else fixed), a **sweep runner** that materializes each point into a real
root `CoordinareConfiguration`, injects it into the 134 substrate (new
backward-compatible seam: `run_board(config=…)` → `daemon.state["config"]` +
`daemon.state["symphony_configs"]`, mirroring `__main__.py:841/976`), scores it with the 135 scorer, and emits a **versioned sweep
artifact + rendered summary** with ablation deltas or a candidate ranking and an
explicit no-silent-truncation coverage section. This is the harness the Phase 4
optimizer (spec 137) plugs into.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv).
**Primary Dependencies (reused, no new runtime deps)**: pydantic 2.x
(space/sweep models following `bench/artifact.py`/`score.py` conventions),
`CoordinareConfiguration` (`config.py:1703`, root global_config + symphonies —
schema-validating materialized points),
the 134 substrate (`bench/runner.run_board`), the 135 scorer
(`bench/grader.score_run`, `bench/score.Weights`, `bench/noise.NoiseReport`),
pyyaml (space definition file), stdlib `statistics`/`math` (repeat derivation),
structlog. No new external dependencies.
**Storage**: none new. Sweep session dirs under `runs/<ts>-sweep/<point-id>/<repeat>/`
(each holding the 134 `run.json` + 135 `score.json`), with `sweep.json` +
`sweep-report.md` at the session root. Committed data: a default search-space
definition + minimal baseline under `benchmarks/spaces/`.
**Testing**: pytest via `make test`; new `tests/unit/test_136_*.py` +
`tests/integration/test_136_sweep_end_to_end.py` (stubbed, deterministic, zero
model cost). SC-006 behavioral-injection proof uses `assignee_filter`
(read from `state["config"]` in `check_board.py:1355` — a configured filter
makes the fake's unassigned cards ineligible, observable from the artifact).
**Target Platform**: Linux (dev + CI), same as coordinare.
**Project Type**: single project (coordinare).
**Performance / termination**: a sweep is bounded by
(points × repeats) × the 134 per-run budget; points run sequentially. Point
count is reported before running (FR-003).
**Scale/Scope**: ablation over ~5-10 dimensions, candidate sets of ~4, repeats
1-10. Not a load test.

## Constitution Check

- **I. Code Quality First** — PASS. No new dependencies. New single-responsibility
  modules in the existing `src/coordinare/bench/` package (`space.py`, `sweep.py`);
  one small backward-compatible parameter on `run_board`. Typed public surfaces.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. Unit tests per module
  (space validation truth table, dotted-path materialization incl. the
  symphonies-by-name addressing, fingerprint distinctness, repeat derivation
  math, coverage accounting), integration test (stub ablation + candidate sweep
  end-to-end), SC-006 behavioral injection test. All offline/deterministic.
  Existing suite stays green (the `run_board` change defaults to today's
  behavior).
- **III. User Experience Consistency** — N/A UI; `scripts/board_sweep.py`
  mirrors `board_score.py` argparse conventions and fails loudly with the
  offending element named (FR-002).
- **IV. Performance by Design** — PASS. Termination bound = points × repeats ×
  the existing 134 budget; the enumerated point count is printed before
  execution so an operator can abort an oversized sweep.
- **V. Clarity Before Action** — PASS. The four material forks (dimension
  semantics, injection seam, stub-mode measurement value, repeats derivation)
  were resolved and recorded in the spec's Clarifications section. Zero
  `NEEDS CLARIFICATION` remain.

**Result**: All gates pass. No Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/136-board-bench-sweep/
├── plan.md              # This file
├── spec.md              # Feature spec
├── research.md          # Phase 0 — verified decisions + rationale
├── data-model.md        # Phase 1 — space/sweep entities
├── quickstart.md        # Phase 1 — how to run an ablation / comparison
├── contracts/
│   ├── search-space.md  # the definition format + validation guarantees
│   └── sweep-artifact.md# the results schema 137 consumes
├── checklists/requirements.md
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/bench/
├── runner.py            # EXTEND — run_board(..., config: CoordinareConfiguration | None = None):
│                        #   sets state["config"] (global) + state["symphony_configs"] (by name,
│                        #   mirrors __main__.py:841/976); fingerprint from materialized content
├── space.py             # NEW — Dimension/Candidate/SearchSpace models, YAML loader,
│                        #   load-time validation (FR-002), dotted-path materializer with
│                        #   symphonies-by-name addressing, config fingerprinting
└── sweep.py             # NEW — SWEEP_SCHEMA_VERSION=1; PointResult/Coverage/SweepArtifact,
                         #   run_sweep() (ablation + candidates), repeat derivation from a
                         #   135 NoiseReport, markdown renderer (sweep-report.md)
scripts/
└── board_sweep.py       # NEW — CLI: `ablate` / `candidates` subcommands mirroring
                         #   board_score.py conventions (+ --space --repeats/--noise-report)
benchmarks/spaces/
├── default.yaml         # NEW — the shipped default space (FR-011): gate dimensions
│                        #   (env_blocked/baseline-repair×3/local-test/ci_gate) + per-role
│                        #   mode choices; candidates conservative/aggressive/cheap/premium
└── baseline.yaml        # NEW — minimal valid root CoordinareConfiguration the space overrides

tests/
├── unit/test_136_space.py            # loader/validation/materialization/fingerprints
├── unit/test_136_sweep.py            # ablation/candidate assembly, repeats math, coverage honesty
└── integration/test_136_sweep_end_to_end.py  # stub ablation + candidates + SC-006 injection proof
```

**Structure Decision**: everything stays in the self-contained
`src/coordinare/bench/` package. The only production-file touch is the
backward-compatible `run_board` parameter (default `None` = today's behavior).
The default space + baseline are committed data under `benchmarks/spaces/`
(runtime inputs, not package code — parallel to how fixture manifests are
file-referenced).

## Complexity Tracking

No constitution violations — section intentionally empty.

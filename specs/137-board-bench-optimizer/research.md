# Research — Spec 137: automated config optimizer

All decisions verified against the code on `main` (post-136 merge, b0bbab6).

## R1 — Search method: memoized evolutionary local search (no new dependencies)

**Decision**: best-first **single-dimension mutation** search with **random
restarts**, seeded (`random.Random(seed)`), memoized by materialized-config
fingerprint (`space.config_fingerprint`) so no configuration is ever evaluated
twice. Loop: evaluate the start point (the baseline's coordinates projected
onto the space, plus every named candidate for the FR-009 comparison); from
the current best, enumerate all single-dimension alternatives, shuffle with
the seeded RNG, evaluate the first non-memoized neighbor; move on improvement;
when a point's whole neighborhood is exhausted without improvement, restart at
a seeded-random unevaluated combination. Stop on: evaluation budget, wall-clock
cap, or full-space exhaustion.

**Rationale**: the issue demands the method be "chosen with justification".
The shipped 136 space is discrete and small (7 boolean gates × concurrency ×
2 mode enums ≈ low hundreds of combinations); at that size a memoized mutation
search reaches any single-coordinate-improvable optimum in O(dimensions) moves
per basin, restarts cover multi-modal surfaces, and memoization makes the
search strictly sample-efficient (every charged evaluation is a new config).
Bayesian optimization earns its complexity on continuous/high-cardinality
spaces and would add a heavyweight dependency (scikit-optimize/optuna/GPy) —
constitution I (minimal dependencies) rejects that for this space. The
evaluator interface keeps the door open: a future BO strategy plugs in behind
the same seam if spaces grow.

**Alternatives considered**: exhaustive grid (no learning; wasteful at
hundreds of points × repeats × real-model cost); simulated annealing (extra
hyperparameters, no benefit at this cardinality); TPE/GP-BO (new dependency,
overkill — above).

## R2 — The evaluator seam (testability fork, verified against 136)

**Decision**: `run_optimizer` takes an `evaluate` callable
(`async (point_id, overrides, config) -> EvalOutcome`); the shipped
`real_evaluator(...)` closes over the loaded space/fixtures/session and runs
materialize → `run_board(config=cfg)` (the 136 seam, `runner.py`) →
`score_run` (135) × repeats, mirroring `sweep._run_point`'s semantics
(per-repeat `score.json` written, mean scalar + mean components, failure
captured not raised). The optimizer always materializes and fingerprints
itself (memoization must not depend on the evaluator), so synthetic
evaluators in tests score `overrides` directly against a planted optimum.

**Rationale**: 136's committed ablation finding — the stub substrate is
signal-free for gate/model dimensions — means convergence can only be *proven*
against an objective with structure. Injecting the evaluator is the smallest
seam that keeps the algorithm honest (identical loop for synthetic and real).

## R3 — Noise handling: enforce 135's condition, don't document it

**Decision**: repeats-per-evaluation come from `sweep.derive_repeats(noise_report,
resolution, max_repeats)` — the 136 derivation, reused. A `--real` search
without `--noise-report` **exits with an actionable error** naming the
prerequisite and the command that produces it
(`board_score.py noise --real …`). Stub searches default to repeats=1, citing
the committed 135 stub measurement (scalar stdev 4.4e-06), recorded in the
artifact as `repeats_source`.

**Rationale**: the issue's ⚠️ gate says an optimizer over a noisy objective
burns budget chasing noise; 135's GO was conditional on a real-substrate
re-measurement. Refusing to start is the only enforcement that survives an
operator who hasn't read the findings.

## R4 — Budget semantics

**Decision**: `max_evaluations` (charged evaluations — cache hits are free,
FR-004) and `max_seconds` (wall-clock, `time.monotonic`). Checked before
starting each evaluation; an in-flight evaluation completes (a board run is
never killed mid-flight; overshoot reported). Baseline + candidates are
charged evaluations (spec Assumption) — they are real runs the comparison
needs. Defaults: `max_evaluations=30`, `max_seconds=3600`.

## R5 — Space size, coverage, and tie-breaks

**Decision**: total distinct combinations = Π over dimensions of
len(set(choices)) — the optimizer's coordinate space is exactly the declared
choices (the baseline value participates only when listed). Coverage =
distinct evaluated fingerprints / total (fingerprint duplicates across
logically distinct coordinates are flagged, mirroring 136). Ties on the
objective: earliest-evaluated wins (deterministic given the seed), noted in
the report. Candidates may propose coordinates outside the dimension grid
(arbitrary overrides) — they are evaluated and compared but never mutated
from, and coverage counts them separately when off-grid.

## R6 — Artifact conventions

**Decision**: `OPTIMIZER_SCHEMA_VERSION = 1`; `OptimizerArtifact` with
round-trip `to_validated_json()` enforcing the FR-010 reconciliation
invariants before write (trace length = charged + cache hits; recommendation
∈ evaluated; coverage counts match distinct fingerprints), `optimizer.json` +
rendered `optimizer-report.md` at the session root — same pattern as
134 `run.json` / 135 `score.json` / 136 `sweep.json`. Stop reasons:
`budget_evaluations | budget_wall_clock | space_exhausted`. Every
`EvaluationRecord` carries provenance
(`baseline | candidate:<name> | restart | mutation-of:<eval-id>`), so the
trace is auditable and best-so-far progression is derivable.

## R7 — CLI shape

**Decision**: `scripts/board_optimize.py` (single command, no subcommands):
`--space --run-dir --seed --max-evaluations --max-seconds
--noise-report [--resolution --max-repeats] | --repeats N --real
--fixtures` + the shared judge/weight flags from `board_score.py`/
`board_sweep.py`. Prints space size + budget up front, the recommendation +
accounting after. `--real` without `--noise-report` exits 2 with the R3
message.

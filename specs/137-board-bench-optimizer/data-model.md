# Data Model — Spec 137: optimizer artifact

Pydantic 2.x models in `src/coordinare/bench/optimizer.py`, following the
134/135/136 conventions (module-level `SCHEMA_VERSION`, round-trip
self-validation before write, rendered markdown beside the JSON).

## A. EvaluationRecord (one trace entry)

| Field | Type | Notes |
|---|---|---|
| `eval_id` | `int` | 1-based trace order |
| `provenance` | `str` | `baseline` \| `candidate:<name>` \| `restart` \| `mutation-of:<eval_id>` |
| `overrides` | `dict[str, Any]` | the point's config overrides (dimension paths → values) |
| `fingerprint` | `str` | materialized-config identity (136 `config_fingerprint`) |
| `cache_hit` | `bool` | True = memoized reuse; **not charged** against the budget (FR-004) |
| `repeats` | `int` | benchmark runs backing this evaluation |
| `score_refs` | `list[str]` | per-repeat `score.json` paths (real evaluator) |
| `mean_scalar` | `float \| None` | the objective; `None` = unrankable |
| `mean_components` | `dict[str, float]` | 135 component means |
| `failed` | `bool` / `error: str` | all-repeats failure (FR-007) — excluded from recommendation |
| `best_so_far` | `float \| None` | objective of the best config after this evaluation |

## B. Recommendation

| Field | Type | Notes |
|---|---|---|
| `eval_id` | `int` | which trace entry won |
| `overrides` / `fingerprint` | | the winning point |
| `mean_scalar` / `mean_components` | | its evidence |
| `tie_note` | `str` | non-empty when ties existed (earliest-evaluated wins, R5) |
| `comparison` | `list[ComparisonRow]` | head-to-head vs baseline + every candidate (FR-009) |

`ComparisonRow`: `label` (`recommendation`/`baseline`/`candidate:<name>`),
`fingerprint`, `mean_scalar`, `delta_vs_recommendation`,
`duplicate_of_recommendation: bool` (fingerprint match flagged, never
double-counted).

## C. BudgetSpent

| Field | Type | Notes |
|---|---|---|
| `max_evaluations` / `charged_evaluations` | `int` | cache hits are free |
| `max_seconds` / `elapsed_seconds` | `float` | wall-clock; overshoot of an in-flight evaluation reported |
| `cache_hits` | `int` | |
| `stop_reason` | `"budget_evaluations" \| "budget_wall_clock" \| "space_exhausted"` | |

## D. OptimizerArtifact (top level → `optimizer.json` + `optimizer-report.md`)

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `int` | `OPTIMIZER_SCHEMA_VERSION = 1` |
| `space_name` | `str` | from the 136 definition |
| `seed` | `int` | full determinism given (space, seed, budget, evaluator) — FR-003 |
| `evaluator_kind` | `"stub" \| "real" \| "synthetic"` | drives the fidelity labeling |
| `repeats` / `repeats_source` / `noise_report_ref` | | 135/136 noise handling (FR-006) |
| `weights` | `Weights` | one 135 weights set per search |
| `space_total_combinations` | `int` | Π len(set(choices)) over dimensions (R5) |
| `distinct_evaluated` | `int` | distinct fingerprints charged |
| `trace` | `list[EvaluationRecord]` | complete, ordered (FR-008) |
| `recommendation` | `Recommendation \| None` | `None` only when every evaluation failed (edge case) |
| `budget` | `BudgetSpent` | |
| `notes` | `list[str]` | fidelity caveats, repeat fallbacks, overshoot, ties |

### Write-time invariants (FR-010, enforced in `to_validated_json()`)

1. `len(trace) == budget.charged_evaluations + budget.cache_hits`
2. `budget.charged_evaluations <= budget.max_evaluations`
3. `recommendation.eval_id` ∈ trace, not failed, not a cache hit duplicate
4. `distinct_evaluated` == count of distinct non-cache-hit fingerprints

## E. Evaluator seam (R2)

```
Evaluator: async (eval_id: int, overrides: dict, config: CoordinareConfiguration)
           -> EvalOutcome {repeats, score_refs, mean_scalar, mean_components, failed, error}
```

`real_evaluator(loaded, fixtures, session, *, repeats, weights, judge,
judge_model, stub, human_login, max_cycles, cost_rate)` builds the shipped
one over materialize → `run_board(config=…)` → `score_run` × repeats.
Synthetic evaluators (tests) score `overrides` against a planted optimum.
The optimizer itself materializes + fingerprints every proposal (memoization
is evaluator-independent).

## F. Relationships

```
LoadedSpace (136) ──coordinates──▶ proposals ──materialize/fingerprint──▶ memo table
      │                                │ evaluate (injectable)
      │                                ▼
      │                 real: run_board(config) × repeats ──▶ score_run (135)
      │                                │
      └── candidates/baseline ──▶ OptimizerArtifact {trace, recommendation, budget, coverage}
                                        └─▶ optimizer-report.md
```

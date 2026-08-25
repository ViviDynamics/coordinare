# Contract: Optimizer Artifact

The versioned result of one budgeted search (FR-008) — the program's end
deliverable. Models in `src/coordinare/bench/optimizer.py`; serialized to
`optimizer.json` (+ rendered `optimizer-report.md`) at the session root
(`runs/<ts>-optimize/<eval-id>/<repeat>/` dirs hold the usual 134 `run.json`
+ 135 `score.json` for real evaluations). Enforced by
`tests/unit/test_137_optimizer.py`.

## Guarantees

1. **Versioned + self-validating**: `schema_version`
   (`OPTIMIZER_SCHEMA_VERSION = 1`), round-trip re-parse before write — the
   134/135/136 convention — plus the FR-010 reconciliation invariants
   enforced at write time: trace length = charged evaluations + cache hits;
   charged ≤ max; the recommendation is an evaluated, non-failed trace entry;
   distinct-evaluated matches distinct charged fingerprints. An artifact that
   does not reconcile refuses to write itself.
2. **Hard budget** (FR-002): `max_evaluations` and `max_seconds` are
   ceilings; an in-flight evaluation may complete (overshoot reported in
   `notes`), no new evaluation starts past either cap. `stop_reason` ∈
   {budget_evaluations, budget_wall_clock, space_exhausted}.
3. **Never twice** (FR-004): memoization by materialized-config fingerprint;
   cache hits appear in the trace but are never charged.
4. **Seed-deterministic** (FR-003): identical (space, seed, budget,
   evaluator) ⇒ identical trace and recommendation. Ties break to the
   earliest evaluation, noted.
5. **Noise handling enforced** (FR-006): `repeats`/`repeats_source`/
   `noise_report_ref` recorded; a real-substrate search without a noise
   report never starts (the CLI refuses with the 135 prerequisite named) —
   135's go/no-go condition as a precondition, not advice.
6. **Failures are data** (FR-007): an all-repeats-failed evaluation is a
   failed trace entry, excluded from recommendation, never fatal; a search
   where everything failed has `recommendation = null` and says so.
7. **Honest comparison** (FR-009): the baseline and every named candidate
   are evaluated in-session under identical repeats/weights and appear in
   `recommendation.comparison` with deltas; fingerprint-identical rows are
   flagged, and "tie within noise" is stated when true (the expected
   stub-substrate outcome).
8. **Fidelity labeling**: `evaluator_kind` ∈ {stub, real, synthetic}; stub
   searches carry the signal-free caveat from the 136 ablation finding in
   `notes` and the rendered report.

## Consumption

Operators read `optimizer-report.md`; tooling reads `recommendation.overrides`
(applying them to the space's baseline reproduces the recommended config —
`fingerprint` verifies it) and `trace` for convergence analysis or a derived
Pareto view over `mean_components`.

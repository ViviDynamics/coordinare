# Contract: Sweep Artifact

The versioned results of one sweep (FR-010) — the surface the Phase 4
optimizer (spec 137) consumes (SC-005). Models in
`src/coordinare/bench/sweep.py`; serialized to `sweep.json` (+ rendered
`sweep-report.md`) at the sweep-session root
(`runs/<ts>-sweep/<point-id>/<repeat>/` per-run dirs hold the usual 134
`run.json` + 135 `score.json`). Enforced by `tests/unit/test_136_sweep.py`.

## Guarantees

1. **Versioned + self-validating**: `schema_version`
   (`SWEEP_SCHEMA_VERSION = 1`), round-trip re-parse before write — same
   conventions as the run artifact, score object, and noise report.
2. **Full accounting** (SC-004): `coverage.declared_points ==
   coverage.scored_points + len(coverage.dropped)` — every declared point is
   either scored or enumerated in `dropped` with its reason (failure,
   unrankable score, baseline-coinciding skip). A point failure never aborts
   the sweep (FR-009).
3. **Deltas against the same sweep's baseline** (FR-006): ablation deltas
   (scalar + per-component means) pair each `(dimension, value)` point with
   the baseline run of the same session — never a baseline from another
   session or weights set.
4. **Ranking honesty** (FR-007): candidate ranking orders by mean scalar;
   `None` scalars rank last and are marked; duplicate materialized
   fingerprints are flagged (`duplicate_of`), not presented as independent
   evidence.
5. **Repeats are data** (FR-008): `repeats` + `repeats_source`
   (`default | operator | noise_report`, with the report path when derived)
   are recorded; per-repeat `score_refs` are kept so means are auditable.
6. **One weights set per sweep**: the 135 `Weights` used for every point are
   embedded; scalars inside one artifact are always comparable.
7. **Fidelity caveat**: `substrate_mode` is recorded; every stub-mode
   report carries the caveat that gate dimensions are exercised structurally
   but most gate deltas are expected ~0 until real-performer runs land
   (spec Assumptions; 134 `real-performer-followup.md`).

## Consumption (spec 137)

The optimizer reads: the space (points it may propose), per-point
`fingerprint`, `mean_scalar`, `mean_components`, and `coverage` — nothing
else. It must treat `dropped` points as unevaluated, not as zero-scored.

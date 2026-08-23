# Contract: Noise Report

The aggregate over N same-config scored runs (FR-008), and the evidence base for
the Phase 4 go/no-go finding (FR-009). Pydantic models in
`src/coordinare/bench/noise.py`; serialized to `noise-report.json` (+ a rendered
`noise-report.md`) at the repeat-session root
(`runs/<ts>-noise/{1..N}/run.json|score.json`). Enforced by
`tests/unit/test_135_noise.py`.

## Guarantees

1. **Versioned + self-validating**: `schema_version` (`NOISE_SCHEMA_VERSION`),
   round-trip re-parse before write — same conventions as the run artifact and
   score object.
2. **Same-config only**: every repeat must share one `config_fingerprint`; a
   mismatch aborts aggregation loudly (the report answers "how noisy is *this*
   config", nothing else).
3. **Failures are first-class**: a repeat that produces no valid artifact/score is
   listed in `failures` with its error and excluded from statistics; the report
   carries both `requested_repeats` and `effective_repeats` — silent drops would
   understate noise (spec US3 scenario 2).
4. **Sample statistics**: per numeric component and for the scalar: mean, sample
   variance (n−1), stdev, min, max, n; `single_sample=True` (variance 0) when
   n < 2. Components missing (`None`) in some repeats count only present values.
5. **Verdict stability**: `card_agreement` maps fixture_id → fraction of repeats
   agreeing with that card's modal verdict category.

## Consumption

- Spec 136 (sweep runner) reads `scalar_stats.stdev` to size repeat-averaging per
  config point.
- Spec 137's go/no-go is the committed finding
  (`specs/135-board-bench-scoring/noise-findings.md`) written from a real
  measurement of this report, including the substrate-fidelity caveat (spec
  Assumptions; 134 `real-performer-followup.md`).

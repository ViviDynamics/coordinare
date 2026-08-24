# Ablation findings — shipped default space, stub substrate

**Date**: 2026-08-23 · **Tool**: `scripts/board_sweep.py` over
`benchmarks/spaces/default.yaml` (baseline `benchmarks/spaces/baseline.yaml`,
tiny fixture, deterministic-only grading, repeats=1/default, weights 1.0/0.1/0.1).
This is the issue-#184 "ablation results" deliverable **at current substrate
fidelity**; re-run with `--real` once the spec-134 real-performer follow-up lands.

## Ablation (10 points declared, 10 scored, 0 dropped — coverage reconciles)

| dimension | value | Δ scalar vs baseline | Δ correctness | Δ wall clock (s) |
|---|---|---|---|---|
| ci-gate | true | −5e-06 | 0 | +0.03 |
| local-test-gate | true | +5e-06 | 0 | −0.03 |
| baseline-prevention | true | −2e-06 | 0 | +0.01 |
| baseline-classification | true | +4e-06 | 0 | −0.03 |
| inherited-repair | true | +1.1e-05 | 0 | −0.07 |
| env-blocked-gate | true | +9e-06 | 0 | −0.05 |
| concurrency | 2 | +7e-06 | 0 | −0.05 |
| implementer-mode | single-premium | +8e-06 | 0 | −0.05 |
| reviewer-mode | single-premium | +3e-06 | 0 | −0.02 |

Baseline scalar 0.999756 (correctness 1.0, wall clock ≈1.47 s); every point
carried a distinct materialized fingerprint (recorded in the sweep artifact).

## Candidates (4 declared, 4 scored, 0 dropped)

Ranking: 1. `premium` · 2. `cheap-models` · 3. `conservative` · 4. `aggressive`
— scalar spread ≈2e-06, i.e. **a timing-jitter tie**, not a real ordering.

## Reading (the honest part)

- **Every delta above is timing noise.** All are within ±1.1e-05, the same
  order as the spec-135 noise measurement's scalar stdev (4.4e-06 at N=5), and
  correctness deltas are exactly 0 everywhere. This is the *expected* stub
  result and the designed validation of the machinery, not evidence that gates
  or model tiers don't matter: the stub bypasses `dispatch_card`, so gate and
  model dimensions cannot influence a stub run's outcome.
- What the run **does** prove: the space validates, all 14 points materialize
  with distinct fingerprints, injected configs reach the daemon's real config
  seams (the integration suite separately proves behavior change via
  `assignee_filter` — SC-006), coverage reconciles with zero drops, and the
  end-to-end ablation + ranking pipeline is exercised for ~20 s of wall clock
  at zero model cost.
- **Not sweepable today** (documented, not invented): blocked-recovery (129,
  env-var-gated) and a security-scan on/off gate (083 has none; the security
  role sweeps via `performers.security.mode`). `max_concurrent_cards` is
  structurally sweepable but behaviorally invisible in stub mode (a card's
  whole lifecycle completes within one cycle).

## Recommendation for spec 137 (the optimizer)

Do not feed stub-substrate ablation deltas to the optimizer — they are noise
by construction. The optimizer's objective becomes meaningful on this space
only under `--real` runs; gate its first real search on (a) the spec-134
real-performer follow-up landing and (b) a fresh `--real` noise measurement
(spec-135 tool) to size repeats-per-point (`--noise-report`, already wired).

# Noise findings — go/no-go for Phase 4 (spec 137, automated optimizer)

**Date**: 2026-08-23 · **Tool**: `scripts/board_score.py noise --repeats 5`
(deterministic-only grading; judge disabled — no judge endpoint was in scope for
this measurement) · **Substrate**: spec-134 stubbed performer, built-in
`tiny-multiply` fixture, no config file (`fingerprint=none`).

## Measurement (N = 5, one fixed config, 5/5 repeats effective)

| component | mean | variance | stdev | min | max |
|---|---|---|---|---|---|
| correctness_rate | 1.0 | 0 | 0 | 1.0 | 1.0 |
| harness_failure_rate | 0.0 | 0 | 0 | 0.0 | 0.0 |
| wall_clock_seconds | 1.7176 | 6.57e-4 | 0.0256 | 1.6810 | 1.7441 |
| **scalar** | **0.999714** | **1.93e-11** | **4.39e-6** | 0.999709 | 0.999720 |

Per-card verdict agreement: `tiny-multiply` 1.000 (5/5 modal).

## Reading

- **Verdict layer is fully stable**: correctness and per-card agreement showed
  zero variance across repeats. On this substrate the correctness component —
  the dominant term of the objective (`w_c=1.0` vs `0.1/0.1`) — is
  deterministic, as designed (the stub applies known solution files; the fake's
  CI runs real pytest on them).
- **Timing is the only live noise source**, stdev ≈ 26 ms on a ~1.7 s run
  (≈1.5% CV), which the default `w_time=0.1 / time_budget=600 s` weighting
  compresses to a scalar stdev of ~4.4e-6 — four orders of magnitude below any
  step an optimizer would need to resolve (a single card verdict moves the
  scalar by ≥ 0.5 at one gradeable card, ~0.2 at five).

## Recommendation: **GO — conditional**

The objective as constructed is stable enough for automated search (spec 137):
the components an optimizer would climb (correctness, harness rate) are exactly
the stable ones, and the noisy one (time) is both small and down-weighted.
Single-repeat evaluation per config point is sufficient **on this substrate**.

**Conditions / caveats (material):**

1. **Substrate fidelity** — this measurement runs the *stubbed* performer:
   spec-134's real-performer path is deferred
   (`specs/134-board-sim-benchmark/real-performer-followup.md`). Real
   performers introduce the dominant stochastic source (model sampling), which
   this measurement cannot see. **Re-run this exact tool once the 134
   follow-up lands**; expect correctness_rate variance > 0 there, and size
   repeat-averaging for spec 136 from that report's `scalar_stats.stdev`
   (the sweep runner should treat repeats-per-point as data-driven, not fixed).
2. **Judge noise unmeasured** — grading here was deterministic-only. When the
   LLM judge is enabled (temperature 0, but self-hosted backends are not
   bit-reproducible), the agree-to-PASS composition can flip borderline cards.
   The `noise` subcommand accepts the judge flags; measure judged noise before
   letting the optimizer run with judging enabled.
3. **Fixture breadth** — one tiny fixture exercises one lifecycle path. The
   whole-lifecycle fixtures in `ViviDynamics/conductor-bench` should be
   re-measured with this tool as they come online; verdict agreement per
   fixture is the early-warning column.

**Bottom line for spec 136/137 planning**: build the sweep (136) with
per-point repeat count read from a noise report rather than hardcoded; gate
the optimizer (137) on a green re-measurement under real performers.

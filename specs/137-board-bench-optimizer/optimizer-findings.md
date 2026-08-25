# Optimizer findings — spec 137 (SC-005)

**Date**: 2026-08-24 · **Tool**: `scripts/board_optimize.py` over
`benchmarks/spaces/default.yaml` (512 grid configurations + 4 candidates).

## 1. Convergence evidence (synthetic objective — the proof)

The search algorithm is proven against synthetic planted-optimum objectives in
`tests/unit/test_137_optimizer.py` (the stub substrate is signal-free — 136
finding — so this is where convergence is *demonstrable*):

- Finds the planted optimum on the test space for **seeds 1, 2, 3**, with
  monotonically non-decreasing best-so-far, within a budget of the full space
  or less (SC-001).
- Identical (space, seed, budget, evaluator) ⇒ **byte-identical traces and
  recommendations** (SC-002).
- **No fingerprint is ever evaluated twice**; cache hits are recorded and
  uncharged. Hard budget (evaluations + wall-clock) verified as a ceiling
  with the stop reason recorded; full-space exhaustion stops early.
- Failed evaluations are recorded and skipped, never fatal; all-failed
  searches emit **no recommendation** rather than a bogus one; ties break
  deterministically to the earliest evaluation, noted.

## 2. Stub-substrate end-to-end search (plumbing validation — honestly labeled)

`--seed 1 --max-evaluations 25`, tiny fixture, deterministic-only grading,
repeats=1 (source: default, per the committed 135 stub measurement):

- **25/25 evaluations charged** (0 cached), coverage 25/512, stop reason
  `budget_evaluations`; artifact reconciles exactly.
- "Recommendation" (two 090 gates flipped on) led the baseline by **4.4e-05**
  and the four candidates by ~7e-06 — **entirely timing jitter**, an order of
  magnitude below nothing meaningful: gate/model dimensions carry no signal on
  the stubbed substrate (the stub bypasses dispatch; 136 ablation finding).
  The artifact and report carry this caveat in `notes`. This run validates
  the loop, the evaluator, the budget accounting, and the head-to-head
  machinery — it does **not** recommend a production configuration.

## 3. Standing instruction for the first real recommendation

The 135 go/no-go condition is **enforced in the tool**, not just documented:
`board_optimize.py --real` refuses to start without `--noise-report`. The
sequence for a real recommendation, once the spec-134 real-performer
follow-up lands:

```bash
# 1) measure real-substrate noise (135):
python scripts/board_score.py noise --repeats 5 --real --fixtures <manifest>
# 2) search with repeats derived from that measurement (136's rule):
python scripts/board_optimize.py --space benchmarks/spaces/default.yaml --real \
  --noise-report runs/<ts>-noise/noise-report.json \
  --seed 1 --max-evaluations 50 --max-seconds 14400
```

Budget guidance: at 512 grid configurations, a 50-evaluation budget covers
~10% of the space; the memoized mutation search spends it on the
locally-improving frontier rather than uniformly. Judge-enabled searches
should first measure judged-mode noise (135 findings §caveat 2).

## Program status (specs 134–137)

All four phases are now landed: substrate (134), scoring + noise (135),
search-space + sweep (136), optimizer (137). The one open dependency for real
end-to-end optimization is the 134 real-performer follow-up
(`specs/134-board-sim-benchmark/real-performer-followup.md`).

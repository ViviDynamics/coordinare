# Quickstart — Spec 137: automated config optimizer

## Run a search over the shipped default space (stub substrate, free)

```bash
.venv/bin/python scripts/board_optimize.py \
  --space benchmarks/spaces/default.yaml \
  --run-dir runs --seed 1 --max-evaluations 20
```

Prints the space size and budget up front; runs the seeded evolutionary
search (baseline + candidates first, then best-first single-dimension
mutations with random restarts, memoized by fingerprint); writes
`runs/<ts>-optimize/optimizer.json` + `optimizer-report.md` with the full
trace, budget accounting, coverage, stop reason, and the recommendation
compared against baseline/candidates.

> Stub caveat (recorded in every stub artifact): gate/model dimensions carry
> no signal on the stubbed substrate (136 ablation finding) — stub searches
> validate the machinery; expect "tie within noise" comparisons.

## Real-substrate search (enforced precondition)

```bash
# 1) measure noise on the real substrate first (135's go/no-go condition):
.venv/bin/python scripts/board_score.py noise --repeats 5 --real [--fixtures …]

# 2) then search, with repeats derived from that measurement:
.venv/bin/python scripts/board_optimize.py \
  --space benchmarks/spaces/default.yaml --real \
  --noise-report runs/<ts>-noise/noise-report.json \
  --seed 1 --max-evaluations 50 --max-seconds 14400
```

`--real` without `--noise-report` is refused with an error naming this
prerequisite. `--resolution` / `--max-repeats` tune the derivation
(136's rule: `ceil((stdev/resolution)²)`, capped).

## Budget

`--max-evaluations` (default 30; cache hits are free) and `--max-seconds`
(default 3600). An in-flight evaluation completes; overshoot is reported.
`stop_reason` tells you what ended the search.

## Judge / weights

Same flags as `board_score.py`/`board_sweep.py`
(`--judge-base-url --judge-model --judge-api-key-env`, `--w-*`,
`--cost-budget --time-budget`); one weights set per search.

## Reproducing a recommendation

Apply `recommendation.overrides` from `optimizer.json` to the space's
baseline (the 136 materializer); the recorded `fingerprint` must match.

## Tests

```bash
make test        # includes tests/unit/test_137_optimizer.py
.venv/bin/pytest tests/integration/test_137_optimizer_end_to_end.py
```

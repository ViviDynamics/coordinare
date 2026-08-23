# Quickstart — Spec 135: scoring + noise characterization

## Score an existing run (deterministic-only)

```bash
.venv/bin/python scripts/board_score.py score \
  --run-dir runs/20260823-120000 \
  --fixtures path/to/manifest.yaml        # omit to use the built-in tiny fixture
```

Writes `runs/20260823-120000/score.json` and prints the scalar + component vector.
Re-running reproduces the same score (timestamps aside).

## Score with the LLM judge (agree-to-PASS)

```bash
.venv/bin/python scripts/board_score.py score \
  --run-dir runs/20260823-120000 \
  --judge-base-url http://litellm-host:4000/v1 \
  --judge-model spark/gpt-oss:120b \
  --judge-api-key-env LITELLM_API_KEY
```

PASS then requires the deterministic contract **and** judge agreement. A judge
error on a card falls back to deterministic-only for that card, visibly in
`judge_reason`.

## Measure noise (N repeats of one fixed config)

```bash
.venv/bin/python scripts/board_score.py noise \
  --repeats 5 \
  --run-dir runs \
  [--fixtures manifest.yaml] [--config config.yaml] [--real] [judge flags]
```

Creates `runs/<ts>-noise/{1..5}/run.json|score.json` plus `noise-report.json` and
a human-readable `noise-report.md`: per-component mean/variance/stdev, scalar
stats, per-card verdict agreement, and any failed repeats with the effective N.

## Weights

`--w-correctness 1.0 --w-cost 0.1 --w-time 0.1 --cost-budget 1.0 --time-budget 600`
— embedded in every score object; only compare scalars across equal weights.

## The committed go/no-go finding

`specs/135-board-bench-scoring/noise-findings.md` records the Phase 4
recommendation from a real N=5 measurement, with the substrate-fidelity caveat
(spec-134's real-performer path is deferred; see
`specs/134-board-sim-benchmark/real-performer-followup.md`).

## Tests

```bash
make test        # includes tests/unit/test_135_*.py
.venv/bin/pytest tests/integration/test_135_score_end_to_end.py  # stub run → score, free
```

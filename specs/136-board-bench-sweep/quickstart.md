# Quickstart — Spec 136: config search-space + sweep runner

## Run an ablation study over the shipped default space (stub, free)

```bash
.venv/bin/python scripts/board_sweep.py ablate \
  --space benchmarks/spaces/default.yaml \
  --run-dir runs
```

Prints the enumerated point count first (baseline + one run per non-baseline
choice), runs each point sequentially through the 134 substrate with the
materialized config injected, scores each with the 135 scorer, and writes
`runs/<ts>-sweep/sweep.json` + `sweep-report.md` with the per-dimension
marginal-effect table and the coverage section.

## Rank the curated candidates

```bash
.venv/bin/python scripts/board_sweep.py candidates \
  --space benchmarks/spaces/default.yaml \
  --run-dir runs
```

Runs each named candidate (conservative / aggressive / cheap-models / premium)
and reports a head-to-head ranking by scalar with component vectors.

## Repeats per point

```bash
# fixed:
... ablate --space … --repeats 3
# derived from a spec-135 noise report (recommended):
... ablate --space … --noise-report runs/<ts>-noise/noise-report.json \
    [--resolution 0.01] [--max-repeats 10]
```

The artifact records the repeat count and its source. A noise report without
usable scalar stats falls back to the default (1) and says so.

## Real performers

Add `--real` (same substrate flag as `board_bench.py`). Stub-mode reports
carry an explicit fidelity caveat: gate dimensions are exercised structurally
but most gate deltas are expected ~0 until the 134 real-performer follow-up
lands.

## Judge / weights

The `board_score.py` judge and weight flags apply unchanged
(`--judge-base-url --judge-model --judge-api-key-env`,
`--w-* --cost-budget --time-budget`); one weights set governs the whole sweep.

## Writing your own space

See `contracts/search-space.md` for the YAML format and validation rules;
start from `benchmarks/spaces/default.yaml`.

## Tests

```bash
make test        # includes tests/unit/test_136_*.py
.venv/bin/pytest tests/integration/test_136_sweep_end_to_end.py
```

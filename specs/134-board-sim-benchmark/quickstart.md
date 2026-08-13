# Quickstart: Board-Simulation Benchmark — Phase 1

## What this gives you

Run the real coordinare daemon end-to-end against a **simulated** GitHub board and
get one structured `run.json`. Only the GitHub API is faked; performers, git, and CI
are real.

## Prerequisites

- The coordinare dev environment (`make` targets available; `.venv` via `uv`).
- A coordinare config YAML whose repo URL points at a `file://` bare repo the fake
  materializes (the runner sets this up from the fixture manifest).
- The sibling `ViviDynamics/conductor-bench` repo cloned as `../conductor-bench`
  (for real whole-lifecycle fixtures; see `scripts/bench_setup.sh` precedent). The
  tiny built-in integration fixture needs no external repo.

## Run it (opt-in, real performers)

```bash
python scripts/board_bench.py \
  --config path/to/bench.yaml \
  --fixtures path/to/fixture-manifest.yaml \
  --run-dir runs/
```

Produces `runs/<ts>-<confighash>/run.json` (+ `raw/` with per-dispatch performer
summaries and per-CI pytest output). The run is bounded by `--max-cycles` and
`--max-wall-clock`; it always terminates and always emits an artifact.

Inspect:

```bash
jq '.totals, (.cards[] | {title, final_state, merged: .merge.merged})' \
  runs/<ts>-<confighash>/run.json
```

## The automated test (deterministic, free)

```bash
make test        # unit + contract: protocol conformance, fake behavior, artifact schema
make test-all    # adds the stubbed-performer end-to-end integration test
```

`tests/integration/test_134_end_to_end.py` seeds one tiny fixture, stubs the
performer via `node_overrides` (no model calls, no cost), drives the daemon to
terminal, and asserts the loop closes and `run.json` validates.

## How it works (one paragraph)

`runner.run_board()` loads the config (`ProjectConfiguration.from_yaml`),
materializes a `file://` bare repo per fixture, and registers cards in a
`FakeGitHubService(approver=gates_green)`. It builds the graph
(`CoordinareGraphBuilder().build()`), constructs
`CoordinareDaemon(..., max_cycles=N, sleep_func=<no-op>, state_store=None)`, injects
the fake at `daemon.state["github_service"]` (plus a recording-wrapped performer
service and a no-op notifier), and runs `await daemon.start()`. Cards flow through
the normal lifecycle; the fake runs real pytest for CI and does real local git
merges; the `gates_green` approver inserts an APPROVED review once gates + CI are
green so the merge path proceeds. On termination the writer builds the artifact from
the fake's event log + the recording wrapper, validates it, and tears everything
down.

## Gotchas

- Leave `symphony_configs` empty (single-symphony harness) or the injected fake gets
  swapped out per cycle.
- The fake must never raise — a raise aborts `daemon.start()`.
- Cost in the artifact is a token×rate **estimate** (`cost_estimated: true`), not
  authoritative proxy USD.

# Quickstart: Rank Backend Harnesses Per Role (161)

## What this gives you

Which backend harness actually works for each coordinare role, with the model held
constant, and evidence for why.

## 1. Roll up an existing run (no inference host needed)

The per-role rollup reads a run artifact that already exists on disk. This is the fastest
way to get value and needs nothing running.

```bash
.venv/bin/python -m coordinare.bench.harness_rollup runs/<timestamp>-<hash>/run.json
```

Writes `harness_rollup.json` beside the `run.json` and prints a table:

```
role            backend        disp  credit  defect  env  incon  defect_rate
----------------------------------------------------------------------------
assessor        junie             3       1       2    0      0        0.667
env_bootstrap   opencode          2       0       0    1      1
reviewer        openclaw          3       3       0    0      0        0.000
tech_writer     hermes            1       1       0    0      0        0.000
```

(Real output, captured from the CLI. Note `env_bootstrap`'s **blank** rate versus
`reviewer`'s `0.000` — see below.)

Read it like this:

- **`defect_rate`** is the harness failing to produce usable output. Lower is better.
- A **negative role verdict is credit, not a defect.** A reviewer emitting
  `changes_requested` was doing its job, so it counts as credit. This is the whole point
  of the feature.
- **`env`** is environment failure, excluded from the rate entirely.
- **`incon`** is inconclusive (no terminal marker, usually a budget or teardown cut).
  A row that is mostly inconclusive is not evidence, and the rate is withheld rather than
  reported as zero.
- A blank `defect_rate` means **no conclusive dispatches**. That is "nothing known", which
  is different from `0.000` meaning "measured flawless".

## 2. Sweep the harness for a role

`benchmarks/spaces/harness.yaml` declares harness dimensions. Unknown harness names are
rejected when the space loads, not halfway through a long sweep:

```yaml
dimensions:
  - name: reviewer-harness
    role: reviewer
    choices: [openclaw, claude_code, codex]
```

Run the sweep with the existing sweep entrypoint, pointing it at this space. Each point
overrides only that role's harness and holds everything else at the baseline.

## 3. Rank and get a recommendation

```bash
.venv/bin/python -m coordinare.bench.harness_rank runs/<ts>-<hash>/run.json [more/run.json ...]
```

Both entrypoints take **run.json paths**, not a directory. Pass several to pool them
(`runs/*/run.json` works). Output goes beside the first artifact given.

Emits a per-role ranking with an explicit verdict:

- `ranked` — a clear winner, with the evidence cited
- `tie` — candidates within the noise band; it will not invent a winner
- `insufficient_evidence` — fewer than two harnesses with enough conclusive dispatches,
  or a pair under the minimum (default 3, configurable)
- `no_viable_harness` — every candidate failed. It reports this rather than crowning the
  least-bad option.

Real output from a single artifact where each role had only ONE harness:

```
assessor: INSUFFICIENT_EVIDENCE
  1. junie          scalar=0.3328  credit=1/3 conclusive  defects=2  runs=1

reviewer: INSUFFICIENT_EVIDENCE
  1. openclaw       scalar=0.9995  credit=3/3 conclusive  defects=0  runs=1

advisory only: no live configuration was modified
```

That is the guard working, not a bug: a role with one harness gets
`insufficient_evidence` because there is nothing for that harness to be better than. To
get an actual ranking you need a sweep that varies the harness (step 2), producing several
artifacts per role.

**The ranking is advisory.** It never writes live deployment config. Applying a winner to
`config.yaml` stays a human decision.

## Prerequisites

- Steps 1 and 3 work offline against recorded artifacts.
- Producing *new* runs (to feed step 3) needs the 151 real-performer path and a healthy
  self-hosted inference host. That host is a hard dependency for fresh data only.

## Gotcha

Do not rank on the artifact's `status` field. It collapses `changes_requested` and
`malformed_output` into the same `"failed"` value, which would penalize a reviewer for
correctly rejecting bad code. Classification uses `terminal_marker`; see
`contracts/outcome-classification.md`.

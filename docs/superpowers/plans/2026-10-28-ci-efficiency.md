# CI efficiency implementation plan

**Goal:** Faster PR feedback with the same tests, coverage floor and check names.

**Scope:** Work only in the provided ci-efficiency worktree at 8ecec0db. Commit locally. Do not push, publish, change settings, restart Docker or prune Docker.

**Evidence and design:** Read both workflows, Makefile, pytest configuration, conftests and local preflight. Inspect six successful PR 355 runs and four successful main runs, matching the branch run list's head SHA to run details. Separate initial scheduling, dependent scheduling and job execution. Benchmark the entire unit suite serially and with xdist 2, 4 and auto workers using Python 3.12. Preserve integration serialization because it uses external processes and Docker.

Alternatives considered: increase the shard count, introduce worker parallelism, or improve the existing four-file-shard duration model. Refreshing measured weights is the first choice because it retains every test and changes no check names. Worker parallelism depends on benchmark results. Consolidating Test and Coverage is valuable but requires preserving the plain-test signal, OTLP verification and coverage artifacts, so assess separately after measurement.

## Tasks

- [ ] Add median aggregation across independent JUnit report sets to scripts/ci_durations.py. Preserve existing per-run summation and browser behavior. Test repeated observations, missing files, setup/teardown duration inputs and deterministic CLI output.
- [ ] Refresh non-browser tests/ci-durations.json from four successful main runs. Keep existing browser weights. Validate exact, nonoverlapping file coverage and replay all four historical runs against the old and new assignments. Collect coverage JUnit artifacts in both workflows for a future coverage-specific refresh.
- [ ] Inspect slow tests and fixtures, fixed ports, global state and environment coupling. Run unit benchmarks serial, 2 workers, 4 workers, auto and a repeated parallel run. Adopt parallelism only if repeated runs pass and integration coverage stays intact.
- [ ] Implement any further measured low-risk gain justified by the evidence, with a regression check and a separate local commit. Record rejected changes and tradeoffs in the report.
- [ ] Run relevant suites in this worktree, lint and workflow validation. Review the whole diff, record benchmark failures honestly, and leave a written report with before measurements, estimated savings, risks and unpublished commit IDs.

## Review focus

- Every discovered file is still assigned exactly once, including new files without timing data.
- Repeated runs must not multiply one file's weight; browser times within a run must still add.
- Coverage artifacts must retain hidden files and combine with the existing 90 percent branch coverage floor.
- A test failure must still fail its original named check. Do not silently skip required checks.
- Local preflight is an installed upstream skill. Inspect it but do not edit its installed scripts.

The user's request authorizes investigation and implementation without another design approval. Final publication is explicitly reserved for the maintainer.

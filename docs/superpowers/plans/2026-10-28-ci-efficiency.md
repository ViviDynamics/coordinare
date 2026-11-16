# CI efficiency implementation plan

**Goal:** Faster PR feedback with the same tests, coverage floor and check names.

**Scope:** Work only in the provided ci-efficiency worktree at 8ecec0db. Commit locally. Do not push, publish, change settings, restart Docker or prune Docker.

**Evidence and design:** Read both workflows, Makefile, pytest configuration, conftests and local preflight. Inspect six successful PR 355 runs and four successful main runs, matching the branch run list's head SHA to run details. Separate initial scheduling, dependent scheduling and job execution. Benchmark the entire unit suite serially and with xdist 2, 4 and auto workers using Python 3.12. Preserve integration serialization because it uses external processes and Docker.

Alternatives considered: increase the shard count, introduce worker parallelism, or improve the existing four-file-shard duration model. Refreshing measured weights is the first choice because it retains every test and changes no check names. Worker parallelism depends on benchmark results. Consolidating Test and Coverage is valuable but requires preserving the plain-test signal, OTLP verification and coverage artifacts, so assess separately after measurement.

## Tasks

- [x] Add median aggregation across independent JUnit report sets to scripts/ci_durations.py. Preserve existing per-run summation and browser behavior. Test repeated observations, missing files, setup/teardown duration inputs and deterministic CLI output.
- [x] Refresh non-browser tests/ci-durations.json from four successful main runs. Keep existing browser weights. Validate exact, nonoverlapping file coverage and replay all four historical runs against the old and new assignments. Collect coverage JUnit artifacts in both workflows for a future coverage-specific refresh.
- [x] Inspect slow tests and fixtures, fixed ports, global state and environment coupling. Run unit benchmarks serial, 2 workers, 4 workers, auto and a repeated parallel run. Adopt parallelism only if repeated runs pass and integration coverage stays intact.
- [x] Implement any further measured low-risk gain justified by the evidence, with a regression check and a separate local commit. Record rejected changes and tradeoffs in the report.
- [x] Run relevant suites in this worktree, lint and workflow validation. Review the whole diff, record benchmark failures honestly, and leave a written report with before measurements, estimated savings, risks and unpublished commit IDs.

## Review focus

- Every discovered file is still assigned exactly once, including new files without timing data.
- Repeated runs must not multiply one file's weight; browser times within a run must still add.
- Coverage artifacts must retain hidden files and combine with the existing 90 percent branch coverage floor.
- A test failure must still fail its original named check. Do not silently skip required checks.
- Local preflight is an installed upstream skill. Inspect it but do not edit its installed scripts.

The user's request authorizes investigation and implementation without another design approval. Final publication is explicitly reserved for the maintainer.

## Completion evidence

- Unit benchmark: serial 16309 passed, 18 skipped, 805.86 seconds. Four new CLI regressions explain 16313 passes in the later runs: 2 workers 422.22 seconds; 4 workers 228.52 seconds and repeat 220.56 seconds. Auto used 20 workers and failed the unchanged 50 ms force-poll budget at 132.2 ms; xdist remains an uncommitted experiment.
- Final targeted verification with coverage: 49 passed. Fresh locked-first environment: 20 passed. Focused fixture profile: 220 passed. Performer rerun with corrected worktree-local temporary layout: 3143 passed in 90.71 seconds.
- Ruff passed; baseline ratchet held; mypy passed for 321 files; actionlint passed with the pre-existing intentional SC2086 warnings filtered.
- Independent read-only whole-branch review approved with no Critical or Important findings. Documentation provenance wording was clarified; the task-specific token path was retained because the user explicitly requires it.
- Historical replay and local timings are measurements. The estimated 60 to 90 seconds faster GitHub green remains unverified until the maintainer authorizes publication.
- Kept all check names and the 90 percent combined coverage floor. Deferred consolidation, more shards, xdist, Docker caching and path filtering for the reasons in the report.

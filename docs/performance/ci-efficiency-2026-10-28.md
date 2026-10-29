# CI efficiency measurements, 2026-10-28

Worktree: `coordinare-ci-efficiency`, branch `ci-efficiency`, starting commit `8ecec0db0376d4c1a54b445022ad0b4b51295dba`. All changes are local and unpublished. No repository settings or other checkout files were changed. Docker was not restarted or pruned.

## CI sample and critical path

Read-only GitHub CLI requests used the requested token file. Runs were selected by branch, then their `headSha` values were checked against run details. These are historical successful runs on different commits, not evidence that an unpublished change has passed CI.

Six PR 355 runs, newest first: [40783079478](https://github.com/ViviDynamics/coordinare/actions/runs/40783079478), [40762281963](https://github.com/ViviDynamics/coordinare/actions/runs/40762281963), [40744693979](https://github.com/ViviDynamics/coordinare/actions/runs/40744693979), [40703326636](https://github.com/ViviDynamics/coordinare/actions/runs/40703326636), [40682822842](https://github.com/ViviDynamics/coordinare/actions/runs/40682822842), [40669263443](https://github.com/ViviDynamics/coordinare/actions/runs/40669263443). Their heads were `170bb370`, `d1fe1118`, `90643f2f`, `3d25b280`, `a0e33db9`, `da2989f9`.

Full-green wall times were 534, 499, 538, 528, 565 and 548 seconds, median **536 seconds (8m56s)**. Coverage shards followed by Coverage combine and Build Success determined the end in all six runs. Initial Lint/Test/Coverage starts were 2 to 4 seconds after workflow creation. Coverage combine started 2 to 4 seconds after the last shard ended, then took 17 to 42 seconds. These samples show little scheduling delay. GitHub's job `startedAt` is not a direct measurement of runner queue time, so these are scheduling offsets, not an invented queue metric.

Per-shard execution steps in seconds, in shard order 1/2/3/4:

- 40783079478: Test 152/316/202/236; Coverage 260/492/429/364.
- 40762281963: Test 176/401/351/189; Coverage 353/382/443/419.
- 40744693979: Test 231/359/300/225; Coverage 342/461/434/401.
- 40703326636: Test 207/332/216/169; Coverage 324/475/341/415.
- 40682822842: Test 209/379/174/284; Coverage 292/519/291/358.
- 40669263443: Test 195/286/259/293; Coverage 315/497/373/462.

For the newest run, the slowest Test step was 2.08 times the fastest; Coverage was 1.89 times. The actual Coverage job lengths, including setup and artifact handling, ranged from 276 to 505 seconds.

Four main runs: [40252018700](https://github.com/ViviDynamics/coordinare/actions/runs/40252018700) at `8ecec0db`, [40156200640](https://github.com/ViviDynamics/coordinare/actions/runs/40156200640) at `b5c8bb2b`, [40103725042](https://github.com/ViviDynamics/coordinare/actions/runs/40103725042) at `ccd7bb97`, and [39948244003](https://github.com/ViviDynamics/coordinare/actions/runs/39948244003) at `0273887c`. Full release completion took 841/790/732/753 seconds, median 771.5 seconds. Main's critical path is Coverage, tag creation, native base/full image builds, manifests, release artifact smoke, installer smoke, attestation/upload, publication. At the starting commit, Coverage finished at 482 seconds and publication at 840 seconds. Main release time includes this required downstream chain; it is not comparable to PR green as if they ran the same jobs.

## Sharding change and estimated savings

The splitter already uses longest-first greedy assignment of entire files. Existing file weights come from JUnit seconds, with fallback to the number of test definitions. Parametrized test cases are not counted individually by that fallback. There is no need to add pytest-split.

At the starting commit, 905 of 965 non-browser files had measurements. The 60 missing files received 756 seconds of count-based weight despite most taking much less. This mixed seconds and counts. Fresh observations cover 961 files; the four remaining files have no normally selected cases and retain the existing fallback. Every file still appears exactly once. Browser weights remain unchanged.

Implemented `scripts/ci_durations.py --median RUN_DIR...` to sum JUnit reports within each run and take per-file medians across independent runs. Missing observations are not zeros. Missing run directories fail before writing timing data. Default summation and duplicate-report handling are preserved. Refresh data came only from the four main runs, using this worktree's existing files. PR 355's different branch was used for comparison, not copied into this checkout.

Replay of current files against the four main runs' recorded JUnit durations, oldest to newest, gives these maximum shard workloads before and after:

- 244.6 to 208.0 seconds, 15.0 percent reduction.
- 196.0 to 167.8 seconds, 14.4 percent reduction.
- 236.4 to 193.2 seconds, 18.3 percent reduction.
- 247.6 to 203.4 seconds, 17.9 percent reduction.

As a holdout check, training on only the oldest three runs gives a maximum workload of 203.7 seconds on the newest run, versus the original 247.6 seconds, a 17.7 percent reduction.

This is a historical file-workload replay, not measured new GitHub wall time. JUnit includes test setup, calls and teardown, but excludes collection, process startup and artifact handling. Coverage overhead varies by file. The observed PR coverage steps have 44 to 154 seconds of ideal balancing headroom (slowest minus average), median 85.75 seconds, before accounting for indivisible files. A similar reduction on the PR coverage critical path would save about 60 to 90 seconds; this remains an estimate requiring an eventual CI run. Both workflows now emit coverage JUnit artifacts separately and print the slowest 50 phases so the next refresh can use instrumented observations instead of assuming a uniform coverage multiplier.

Increasing to six or eight shards replays the newest main workload at maximum 136.7 or 106.2 seconds, versus 203.4 seconds with the refreshed four. This remains a runner-count experiment: it increases repeated collection/setup and changes matrix check names. Keep four until the smaller measured change is validated and runner capacity and check requirements are explicitly considered.

## Duplication and caches

Test and Coverage select the same non-browser files with the same default markers. The plain tests cost another 906 to 1117 runner-seconds per PR, median 1039.5 seconds, alongside instrumented execution. Combining them could save about 17.3 runner-minutes per PR. It does not automatically reduce the critical path because Coverage already runs concurrently. A consolidation must preserve OTLP-extra verification and the existing named check failure behavior. It is deferred until the maintainer chooses how the compatibility checks should report and a published CI experiment can validate it.

Across the six PR logs, all **138 uv cache lookups hit**. Main had 33 hits and 19 misses across four runs. Some changes in dependency keys required cold fills; subsequent runs hit. Setup uv usually cost 2 to 4 seconds; Test/Coverage dependency installation cost a median 4 seconds. Logs exposed a different waste: the unlocked root pip install was followed by `uv run` replacing 52 packages in the newest PR shards and 68 in the newest main shards. A four-trial local setup comparison, ordered legacy/locked/locked/legacy, took 5.709/1.851/1.832/3.303 seconds. The first legacy trial included cold downloads; the warm comparison is 3.303 seconds versus approximately 1.84 seconds, a 44 percent reduction. Both variants installed performer extras and successfully imported coordinare, performer and pytest after the normal `uv run` synchronization. Test and Coverage in both workflows now begin with `uv sync --extra dev --frozen`, then install performer extras as before. This saves about 1.5 seconds per warm local job, not minutes of critical-path time. The fresh alternative environment also passed the 20 sharding and release-workflow tests. Performer-only extras remain separately resolved, so this does not claim a unified lockfile for both projects.

Restoring whole virtualenvs offers little benefit here and introduces platform and editable-path risks. Pip has no separately configured cross-job cache.

Playwright runs in its matching official image. Browser install steps cost 3 to 7 seconds, including root dependency setup. Container initialization cost 18 to 28 seconds. There were no browser download messages in the newest PR log. Adding another browser cache is unlikely to repay its restore/upload cost.

Docker has no cross-run layer cache configured. The newest PR log contains only one `CACHED` build step, in kind setup; the base/full image builds still take a median 79.5/109 seconds. Kind separately spends median 50.5/95 seconds building and loading daemon/performer images, 41 seconds creating the cluster and 98.5 seconds verifying the chart installation. Main base builds take 70 to 97 seconds and full builds 113 to 143 seconds. Layer caching is a reasonable later experiment but is not the current PR bottleneck. It needs careful treatment of mutable package installations, base image freshness, image loading and provenance tests. No Node module restore is configured; the relevant npm installations are inside the full Docker image. Dashboard JS unit tests run directly under Node without npm install.

## Parallelism and profiling

The Python 3.12 serial unit baseline passed: **16,309 passed, 18 skipped**, pytest 805.86 seconds and end-to-end process time 813.28 seconds. The 2-worker run passed 16,313 tests with 18 skipped in 422.22 seconds (424.27 seconds including process lifetime); the 4-worker run passed the same cases in 228.52 seconds (230.71 seconds including process lifetime). Automatic workers completed in 119.17 seconds (121.34 seconds including process lifetime), with **one failure**, 16,312 passes and 18 skips. The repeat 4-worker run passed all 16,313 cases with 18 skipped in 220.56 seconds (222.39 seconds including process lifetime). The host has 20 logical CPUs and other ongoing workloads, so these are local comparisons, not GitHub speed guarantees. Auto used 20 workers and failed `tests/unit/dashboard/test_force_poll.py::TestForcePollEndpoint202::test_response_time_under_50ms`: measured client latency was 132.2 ms against a 50 ms budget. The same test passed in serial, 2 and 4 workers. This is observed load sensitivity; its assertion remains intact. No xdist dependency or invocation is committed. A future rollout should first isolate the default event database, bound workers, retain serial integration execution and validate instrumented coverage on actual runners. Four new CLI regression cases were added after the initial serial collection, so later runs include four additional cases.

The slowest serial test was the SQLite filter-index throughput benchmark at 42.83 seconds. Other notable calls were optimizer sweeps at 7.81/7.64 seconds, the activity-log memory flood at 6.04 seconds, a hung-status deadline check at 5 seconds, and intentionally adversarial redaction cases at roughly 2.6 to 4.1 seconds each. Two cancellation tests each spent 2.9 seconds in teardown waiting for deliberately slow background threads. No setup phase appeared in the top 50 phases, whose cutoff was 2.21 seconds. A focused fixture-hook profile of 220 tests passed in 9.65 seconds. Across those tests, tmp_path setup totaled 0.093 seconds; the module RSA fixtures took 0.030 and 0.022 seconds, and the graph invariant guard totaled 0.023 seconds over 94 calls. Stamina and persona-history autouse setup totaled 0.0025 and 0.0019 seconds over 220 calls. These measured fixtures are not the bottleneck; broadening their scopes would add isolation risk for negligible savings.

The Kubernetes egress tests took 20.63 seconds standalone, with 20 seconds spent waiting on fake clients that always report Pending. The current timeout and verdict assertions can be exercised with a virtual event-loop clock instead of waiting in real time. That test-only optimization was applied after all five full unit runs finished, so it does not distort their comparison. The same 28 tests now pass in **0.70 seconds**, versus **20.63 seconds** before. The probe's two-second sleeps advance a clock scoped to a fresh event-loop runner and still yield to executor futures; production code, verdict assertions, cleanup assertions, timeout values and real kind coverage remain intact. This removes about 20 seconds per invocation, roughly 60 runner-seconds per PR across Test, Coverage and Chart. The historical timing weight is retained conservatively until a new CI artifact measures the changed tests; it overestimates this one file's current cost.

The serial unit run also modified the tracked worktree-root `events.db` through daemon startup tests using their default state path. This is concrete shared filesystem state, despite the many tests that correctly use `tmp_path`. The generated database change will be restored locally after the runs and will not be committed. Any xdist adoption must account for this coupling; passing one run alone is insufficient.

Unit fixture inspection found 144 function-scoped fixtures and three module-scoped fixtures, plus inherited function autouse fixtures for stamina retry state, persona-history temporary paths and graph invariants. The module fixtures are a Makefile text read and two RSA key fixtures. These are poor candidates for broad session scoping. Session Docker availability is already cached in the root conftest; image builds in integration and performer conftests are session-scoped but would repeat per xdist worker.

Unit socket listeners use ephemeral port 0 where sockets are actually bound. Many apparent fixed-port occurrences are mock configuration values. Process-local globals, monkeypatches and environment changes are isolated by workers, but this is not proof of universal parallel safety. Keep integration tests serial: the Rails service test launches real subprocesses after releasing free-port probe sockets, and the subprocess/container coexist test builds images and polls Docker host port forwarding. More concurrent load can expose their existing startup sensitivity.

## Path filtering, required checks and preflight

The live `rules/branches/main` response contained deletion, non-fast-forward, linear-history and pull-request rules, with no required-status-check entry. Classic branch protection returned 404. This differs from AGENTS.md's statement that Lint, Test and Build Success gate merges. No settings were changed and all check names are preserved. Build Success itself depends on Lint, Test, Coverage, E2E, Performer, Docker and Benchmark, so advisory labels in documentation do not describe its actual dependency graph.

The release arm64 path filter already exists. A Python-only change can affect the HTTP API, dashboard behavior, daemon image and a real kind installation, so it is not a safe blanket reason to skip E2E or Chart Install. Documentation also has executable content assertions in unit tests. A conservative future filter should identify positively safe docs-only changes, treat unknown paths as relevant, include workflow/scripts/shared-package dependencies, and preserve explicit success checks for required names while failing on filter errors. No new filter is implemented without this dependency evidence.

The installed preflight scripts were read and left unchanged, as AGENTS.md requires upstream edits for installed skills. The repo-specific table currently runs changed unit files, then runs the entire tests tree again when those files change. That duplicates the changed tests locally. A table simplification could remove the first row because the full suite is a superset; it is a separate local-workflow improvement, not a measured CI wall-time gain. No preflight push was attempted.

## Reproducing the evidence

Use the task-specific requested `GH_TOKEN=$(cat ~/Workspaces/.gh_token) gh ...` prefix for every GitHub command. This explicit token location takes precedence over the general organization-workspace instruction. Read run lists with `--branch main` and `--branch 353-sharded-discovery`, match `headSha`, and get `gh run view ID --repo ViviDynamics/coordinare --json jobs,createdAt,startedAt,updatedAt,headSha`. Download only successful runs' `junit-test-*` artifacts into one directory per run, then invoke:

```sh
python scripts/ci_durations.py --median RUN_DIR_1 RUN_DIR_2 RUN_DIR_3 RUN_DIR_4
```

Merge only the desired lane's weights into `tests/ci-durations.json`; downloading no E2E reports must not erase E2E weights. Keep `junit-coverage-*` in separate directories. Retain independent holdout runs for future replay. Raw run JSON, logs, downloaded JUnit, benchmark logs and the worktree-only uv cache are retained in ignored `.ci-efficiency/` for inspection.

Four complete unit runs passed: serial, 2 workers, 4 workers and a repeated 4-worker run. Auto is explicitly a failed experiment, not a passing verification. Existing test behavior was unchanged through the matrix; four added CLI regression cases explain the later count increase. The virtual-clock optimization was applied only after the matrix finished.

Local measurements used Python 3.12.15 on macOS arm64, root `uv sync --extra dev --frozen`, then `uv pip install --python .venv/bin/python -e 'agent/performer[dev]'`. The serial run preceded the worktree-only installation of pytest-xdist 3.8.0. Each complete run used a separate basetemp directory and JUnit output, with exported `COORDINARE_*` variables and `HERMES_CONTEXT_WINDOW` removed. Commands were the following with `WORKTREE` set to this dedicated worktree:

```sh
.venv/bin/pytest tests/unit -q --durations=50 --junitxml=serial.xml --basetemp="$WORKTREE/.ci-efficiency/pytest-serial"
.venv/bin/pytest tests/unit -q --durations=50 -n 2 --junitxml=n2.xml --basetemp="$WORKTREE/.ci-efficiency/pytest-n2"
.venv/bin/pytest tests/unit -q --durations=50 -n 4 --junitxml=n4.xml --basetemp="$WORKTREE/.ci-efficiency/pytest-n4"
.venv/bin/pytest tests/unit -q --durations=50 -n auto --junitxml=auto.xml --basetemp="$WORKTREE/.ci-efficiency/pytest-auto"
.venv/bin/pytest tests/unit -q --durations=50 -n 4 --junitxml=n4-repeat.xml --basetemp="$WORKTREE/.ci-efficiency/pytest-n4-repeat"
```

Reference behavior was checked against [pytest JUnit duration documentation](https://docs.pytest.org/en/stable/reference/reference.html), [xdist distribution documentation](https://pytest-xdist.readthedocs.io/en/latest/distribution.html), and [uv's GitHub Actions guide](https://docs.astral.sh/uv/guides/integration/github/).

## Final verification

The changed sharding, release-workflow, Kubernetes probe and latency cases passed together with coverage enabled: 49 passed in 6.75 seconds. The alternative locked-first environment passed 20 workflow/sharding cases. Ruff passed over `src tests agent/performer/src`; the Ruff ratchet held at 1383 violations across 43 rules; mypy reported no issues in 321 source files. Actionlint passed while filtering only the existing SC2086 intentional file-list splitting warnings. It also passed with ShellCheck disabled; the unfiltered output is retained for transparency. No coverage floor, marker, timeout, check name or assertion was weakened.

The initial separate performer run had 3137 passes and six failures caused by the worktree-local harness layout. The explicit basetemp directory was a sibling of TMPDIR, so QA's temp-containment safeguard correctly refused cleanup, and Git fixtures without their own `.git` discovered the enclosing worktree. Setting TMPDIR to `.ci-efficiency` and GIT_CEILING_DIRECTORIES to that same directory fixed all 12 focused cases. This preserves all assertions and prevents fixture Git discovery from reaching the worktree, while keeping pytest's temporary files in the authorized directory. The complete corrected-layout performer rerun passed: **3143 passed** in **90.71 seconds**, 91.94 seconds including process lifetime. No performer source or assertions were changed.

The independent read-only review approved the implementation with no Critical or Important findings. It independently reproduced all 961 refreshed weights, unchanged E2E values and exact coverage of all 965 non-browser files. Its benchmark-provenance wording concern was clarified. Its suggested token-path change was declined because this task explicitly requires `~/Workspaces/.gh_token`, overriding the general organization instruction. No additional code changes were required.

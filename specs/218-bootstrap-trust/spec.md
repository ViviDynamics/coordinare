# 218: The daemon's success and readiness decisions do not run or trust performer-written code

## Problem

The env-cache directory is written by the bootstrap performer, which runs untrusted code. Three
daemon decisions read that directory, so anything the performer wrote there could decide its own
success. Found in the review of #217 (spec 208).

- **verify.sh.** The clean-context verify ran `verify.sh` from the cache, in a fresh container with
  the cache mounted read-only, and its exit code decided the bootstrap. The performer could rewrite
  it to `exit 0`.
- **`_cache_dir_is_populated`.** It checked entry names only, so a dummy file named like a toolchain
  passed.
- **`_cache_dir_has_activate`.** It gated mounts and dispatch on the existence of `activate.sh`
  alone. A link was refused (219), but a rewritten or emptied file passed.

Review round 2 found two more gaps, both confirmed against the real performer image:

- **The image profile ran before the verify's script.** `Dockerfile.full` sets
  `BASH_ENV=/etc/devenv-activate.sh`. Bash sources it before `bash -s` reads stdin. The profile
  unpacks `debs/*.deb` from the cache, symlinks missing `/usr/bin` entries, sources the cache's
  `activate.sh` and starts `services/services-start.sh`. A planted deb with a fake `ruby` made the
  verify pass.
- **A time-of-check gap on activate.sh.** The daemon checked the cache's `activate.sh`, and then
  docker sourced it a moment later. A writer toggling the file passed 8 of 15 runs.

Review round 3 found the remaining gaps in the completion path:

- **The "container is gone" check failed open.** A `None` handle, and any inspect failure, counted as
  quiet. On Kubernetes the handle is a Pod name, so docker could not answer, and the check passed while
  the Pod still ran.
- **A hung verify retried forever.** A verify that hit its 300 s budget mapped to infrastructure, which
  never spends the verdict budget. A performer could force endless re-bootstraps, and the timed-out
  container kept running.

## Scope

The decisions below are made by the daemon. A performer cannot make them succeed by running code or by
writing text the daemon then trusts.

**What the verify proves, exactly.** The verify confirms that the toolchain the bootstrap installed runs
and reports the expected version. It cannot detect a malicious toolchain. Checking a version executes the
installed binary, so a planted binary that prints the right version passes. That is binary provenance,
tracked in **#234** (a digest or a snapshot), and this PR does not change it. A PASSED verify is not a
security verdict on the toolchain.

This spec also does not cover what a consumer's shell runs from the cache. Consumers run toolchain
binaries and `services-start.sh` from the cache by design (see "Residuals").

## Inventory of decision points

| Decision | Site | Reads | Now |
| --- | --- | --- | --- |
| Clean-context verify (completion, restart, dispatch gate 093) | `verify_env_cache_clean` | the daemon's rendered text, and nothing from the cache copies | Piped on stdin to `bash -s` in a container with the profile disabled and the cache mounted read-only. The activation is inlined, so the cache `activate.sh` is never read by the verify. |
| Consumer mount and dispatch hold (`get_env_volume_for_symphony`, persistent volumes in `_resolve_env_volumes`, `_env_cache_hold`, `_env_readiness_gate`) | `_cache_dir_has_activate` | the cache `activate.sh` | Byte-identical to `EnvCacheState.rendered_activate_sh`. |
| Phantom-success re-bootstrap (`check_and_trigger`) | `_cache_dir_has_activate` | the cache `activate.sh` | The same exact match. |
| Bootstrap success floor (`on_bootstrap_complete`) | `_cache_dir_is_populated` | cache entry names and types | A real, non-link directory beyond the seed set and `services/`, holding at least one entry. |
| Completion verify (`_handle_bootstrap_terminal_status`) | daemon | the runtime's proof that the bootstrap workload is gone | Runs only after `confirm_stopped` proves it. A pass counts. A failed verify or a timeout spends the verdict budget. Any infrastructure failure spends the infrastructure budget. |
| Cache key input (`load_prior_manifest_bounded`) | env_cache | `services/services.json` | Unchanged. See "Residuals". |

The daemon never executes `services/services-start.sh` or `services-health.sh`. The performer runs
them inside its own container (`workspace.run_service_readiness`), and the daemon takes only the
performer's report.

## Design

### The daemon renders and keeps the scripts

`EnvCacheService._build_manifest_artifacts` renders `activate.sh` and then `verify.sh` once, from the
manifest. It stores the text on `EnvCacheState` as `rendered_activate_sh` and `rendered_verify_sh`.
The cache copies are still written, for the performer to read, but the daemon never runs them.

- The text is set only after every write succeeds. `_do_dispatch` clears it before each render. A
  refused or failed write leaves no script on record, and the verify fails closed.
- An empty manifest still gets the daemon-owned scripts (a verify that checks nothing). Before 218 it
  fell back to performer-written scripts that the daemon never checked.
- The text is persisted in the snapshot (see Compatibility), so a restart verifies the daemon's text
  and not the cache copy.

### The verify container runs only the daemon's text

`verify_env_cache_clean` runs:

    docker run --rm -i --name coordinare-verify-<cache>-<id> -e BASH_ENV= -e ENV= \
      -e _DEVENV_SOURCED=1 -e _DEVENV_SKIP_SERVICES=1 \
      -v <cache>:<container path>:ro --entrypoint bash <image> -s

with the rendered `verify.sh` as stdin.

- **`BASH_ENV=` and `ENV=`** stop the image's profile for bash and sh. Bash sources `BASH_ENV` before it
  reads stdin, so this is the part that matters. Verified against the real image: with the defaults a
  planted `/usr/bin/ruby` is on `PATH`; with these flags it is not.
- **`_DEVENV_SOURCED=1`** makes the profile a no-op if a variable is re-exported.
- **`_DEVENV_SKIP_SERVICES=1`** keeps any service start from running.
- **Inlined activation.** The rendered `verify.sh` carries the daemon's `activate.sh` text. It never
  sources `<cache>/activate.sh`, so a copy swapped in after the daemon's check is never read. The
  daemon still checks that the cache copy matches, and a mismatch fails the verify. That check now
  gates mounts and dispatch, not the verdict.

Outcomes of the run:

- **A pass or a failure** is the verify's own verdict (exit 0 or not).
- **A timeout** (300 s, `_VERIFY_TIMEOUT_S`) is a **real failure**. The client is killed and the container
  is removed by its name (`docker rm -f`). The failure says whether the removal was confirmed.
- **Exit 125** means docker could not start the container (the daemon or the image). It is an
  infrastructure failure (None), because it says nothing about the toolchain.
- **A missing docker binary** is an infrastructure failure (None).

### Completion verify runs only after the bootstrap workload is proven gone

The performer reports terminal while its workload may still run, and the clean verify reads the cache
read-only. A live writer would race the verdict. So the success path calls the runtime's own proof,
`confirm_stopped(handle)`, before it verifies. Each runtime answers on its own transport:

- **Docker** (`DockerRuntime.confirm_stopped`). `docker stop` succeeded, or `docker container inspect`
  returned "No such container" (the container was removed by `--rm`). An inspect that fails for another
  reason, or a container still running, is not proof.
- **Kubernetes** (`KubernetesRuntime.confirm_stopped`). The Pod is deleted, then read until it returns
  404 or the budget runs out. A Pod that still reads, or a read that fails for any other reason, is not
  proof. The Pod name is the handle, so docker is never asked about it.
- **Anything else.** A runtime with no `confirm_stopped`, a `None` handle, or an error inside the check
  returns False. Nothing is proven, so the verify does not run.

A False proof fails the bootstrap as an infrastructure failure, and the verify is not run.

### Infrastructure failures have their own budget

An infrastructure failure is one where the verify did not run, or the bootstrap workload could not be
proven gone. It does not spend the verdict budget (`bootstrap_attempts`). It has its own count
(`infra_failures`), persisted with the 088 fields:

- Each one increments `infra_failures` and sets `last_infra_failure_at`, and logs
  `env_cache.bootstrap_infra_failed` with an operator hint.
- The retry cooldown escalates with `bootstrap_attempts + infra_failures` (the 077 cooldown, doubled per
  failure).
- At `_INFRA_FAILURE_CAP` (5) the cache becomes **exhausted** (the existing `bootstrap_exhausted` state).
  Its last error names the infrastructure reason, and the hold message says so.
  `env_cache.bootstrap_infra_exhausted` is logged, and the operator is notified.
- A success clears `infra_failures`. A new spec SHA re-arms the infrastructure budget, as it re-arms the
  088 breaker. A docker fix alone does not re-arm it. A spec change, or a fresh deployment, does.

A real verify failure, including a timeout, spends the verdict budget as before.

### Readiness checks validate content

- `_cache_dir_has_activate(cache_dir, expected_activate=...)` reads `activate.sh` through the anchored
  helper (219) and compares it with the rendered text. Missing, linked, hard-linked, FIFO, oversized
  (256 KiB) and different content all count as no toolchain.
- `_cache_dir_is_populated` requires a toolchain entry that is a real directory (not a link, file or
  FIFO), holds at least one entry, and is not a seed file (`activate.sh`, `manifest.json`, `verify.sh`)
  or `services/`. A dummy-named file, an empty directory and a link to a directory all fail.
- The populated check is a structural floor only. The daemon cannot predict installed toolchain contents.
  The daemon's verify run decides whether the toolchain runs and reports the expected version (see Scope).

## Residuals (accepted, and what closes them)

These are what the daemon does not check. The PR goal is that the daemon's decisions do not run or trust
performer-controlled code or text. Each residual is outside that goal.

- **Installed toolchain provenance (#234).** The verify executes the installed binaries, so a planted
  binary that prints the expected version passes. This is the limit stated in Scope. Closing it needs a
  digest or a snapshot the daemon builds, and that is #234.
- **Consumer shells run cache code.** A consumer container sources the cache's `activate.sh` through the
  image profile, runs toolchain binaries from the cache, and starts `services-start.sh` (the profile's
  spec-117 start-on-activation). This is by design: consumers need the toolchain, and the cache is mounted
  read-only into them. So a performer that wrote the cache can run code in a consumer's shell. Closing this
  is the same provenance design, #234.
- **Service readiness at bootstrap.** The performer decides whether required services started and answered
  (`run_service_readiness`), then reports `env_bootstrap_complete`. The daemon does not run
  `services-start.sh` or `services-health.sh`, and it cannot start services in the read-only clean
  container, because the services keep their data inside the cache. A lying performer can get a cache
  marked complete with a service that is not up. Consumers start their own services through the profile.
  A test pins that the daemon ignores the health script: a rewritten health script changes neither the
  toolchain verify nor its verdict.
- **`services/services.json` in the cache key.** The performer can change it, and that changes only
  whether a bootstrap re-runs. It does not change the spec-file SHA, so it cannot skip a re-bootstrap on a
  spec change.
- **The verify is docker-based on every transport.** The clean verify runs `docker run`, as it did before
  218. A deployment with no docker on the daemon host gets infrastructure failures, which are visible and
  capped, not a false pass.

## Compatibility

- **Schema v35.** This is the same unreleased v35 step. `EnvCacheStateSnapshot` gains
  `rendered_verify_sh`, `rendered_activate_sh` (both `None` by default; both kept byte for byte with
  `StringConstraints(strip_whitespace=False)`), and `infra_failures` (0) and `last_infra_failure_at`
  (None). Older snapshots load unchanged. #147 also takes v35 (`card_lifecycle`), so the rebase makes this
  step v36.
- **One re-bootstrap after upgrade.** A cache bootstrapped before 218 has no rendered script and no
  matching activate record. It fails closed: consumers are held, and the existing activate-missing path
  re-bootstraps it once.
- **Empty-manifest symphonies.** These now get a daemon-owned verify.sh and activate.sh. The bootstrap
  prompt says a coordinare-provided verify.sh exists, with no checklist.
- **The verify container.** The docker run carries `--name` and the four `-e` flags. Operator tooling that
  pattern-matches the verify's docker command must allow them.

## Refusal

A refused operation is never followed. Refusals are logged as before, and the verify returns False with a
reason (`env_cache.clean_verify_refused`, `env_cache.activate_mismatch`). A refused cache dir is reported
through `env_cache.bootstrap_cache_refused` on completion. A bootstrap container that cannot be proven
gone is logged as `env_cache.bootstrap_container_still_running` or `env_cache.bootstrap_quiesce_failed`, and
the verify is not run.

## Tests

Unit (run in the default suite):

- `tests/unit/test_218_bootstrap_trust.py`: the first-round attacks (verify.sh rewritten, activate.sh
  rewritten, dummy-named entries, the health script, the honest end-to-end path, the snapshot round trip).
- `tests/unit/test_218_verify_container.py`: the exact docker argv (the `-e` flags, `--name`), the stdin,
  the inlined activation, and a rewrite of activate.sh during the run that does not change the verdict.
- `tests/unit/test_218_verify_failure_modes.py`: a hung verify is a real failure and its container is
  removed by name; exit 125 and a missing docker are infrastructure; the infrastructure budget caps at 5 and
  exhausts with the reason; a success resets it; real failures still spend the verdict budget; the budget
  persists.
- `tests/unit/test_218_quiesce_transports.py`: the Docker proof (stop succeeds; a real "No such container"
  is proof; another inspect error is not; a running container is not), the Kubernetes proof (a 404 after
  delete is proof; a Pod that still reads is not; a 500 is not), the daemon's fail-closed cases (a Kubernetes
  Pod handle answered by the runtime, a `None` handle, an unknown transport, an error inside the check).
- `tests/unit/test_218_completion_ordering.py`: the container is stopped before verify; a still-running
  container fails closed; a removed container counts as quiet; docker outage is an infrastructure failure.
- `tests/unit/graph/nodes/test_218_dispatch_activate_gate.py`: the dispatch gate refuses a tampered
  activate.sh with the real verify, and the readiness gate's False branch refuses it without running docker.

Opt-in docker lane (not run by default; see "Quality gate" below):

- `tests/integration/test_218_verify_real_image.py` (marker `docker_image`): through real docker, with the
  image named by `COORDINARE_E2E_PERFORMER_IMAGE`. A planted deb with a fake ruby does not pass the verify,
  and five verifies with a writer swapping activate.sh continuously all report the verify's own FAIL (or the
  daemon's mismatch refusal). A module fixture fails when the image is missing, so the lane cannot pass on an
  absent image.

Tests changed because they pinned the old path (fixture and expectation changes only):

- `tests/unit/test_060_env_cache.py`: the mount and persistent-collect tests write the rendered activate; the
  empty-manifest test now expects daemon scripts; the poll-loop tests stub the clean verify and the quiesce
  (both are covered in the new modules). The idle-reap test now runs the real quiesce and asserts the stopped
  container's id is in the `docker stop` argv and that the stop happens before the verify.
- `tests/unit/graph/nodes/test_dispatch_performer.py`: `_ready_env_cache` stores a matching activate, and an
  autouse fixture keeps the 093 gate in its degraded pass-through for these volume tests. The False branch is
  covered by `test_218_dispatch_activate_gate.py`, which does not use that fixture.
- `tests/unit/test_daemon_snapshot_persistence.py`, `test_219_env_cache_root_mounts.py`,
  `test_219_env_cache_safe_writes.py`, `tests/integration/test_env_cache_lifecycle.py`: rendered-script and
  activate fixtures (round 1).
- `tests/unit/test_env_manifest.py`: `test_sources_activate_and_exits` becomes
  `test_inlines_activate_and_exits`, which asserts the cache path is no longer named.
- Version pins (round 1, schema v35): the `CURRENT_SCHEMA_VERSION` assertions and the contract enum.

## Quality gate

No new `skip`, `skipif`, `xfail` or other quality-guard marker is added. The docker lane is selected out by
`tests/conftest.py`, which deselects (does not skip) items marked `docker_image` unless
`COORDINARE_DOCKER_IMAGE_TESTS` is set. The marker is registered in `pyproject.toml`. Because no guarded
pattern is added, this PR needs no entry under "Quality gate changes". Its PR body needs this entry under
"Test selection" instead:

> `docker_image` marker: tests that need docker and a built performer image are deselected unless
> `COORDINARE_DOCKER_IMAGE_TESTS=1` is set (tests/conftest.py). They are not skipped, and a missing image
> fails the run. Run them with `COORDINARE_DOCKER_IMAGE_TESTS=1 COORDINARE_E2E_PERFORMER_IMAGE=<image>
> pytest -m docker_image tests/integration/test_218_verify_real_image.py`.

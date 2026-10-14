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

## Scope

The decisions below are made by the daemon. A performer cannot make them succeed by running code
or by writing text the daemon then trusts. This spec does not cover what a consumer's shell runs
from the cache. Consumers run toolchain binaries and `services-start.sh` from the cache by design.
Binary provenance needs a separate digest or snapshot design, tracked as its own issue.

## Inventory of decision points

| Decision | Site | Reads | Now |
| --- | --- | --- | --- |
| Clean-context verify (completion, restart, dispatch gate 093) | `verify_env_cache_clean` | the daemon's rendered text, and nothing from the cache copies | Piped on stdin to `bash -s` in a container with the profile disabled and the cache mounted read-only. The activation is inlined, so the cache `activate.sh` is never read by the verify. |
| Consumer mount and dispatch hold (`get_env_volume_for_symphony`, persistent volumes in `_resolve_env_volumes`, `_env_cache_hold`, `_env_readiness_gate`) | `_cache_dir_has_activate` | the cache `activate.sh` | Byte-identical to `EnvCacheState.rendered_activate_sh`. |
| Phantom-success re-bootstrap (`check_and_trigger`) | `_cache_dir_has_activate` | the cache `activate.sh` | The same exact match. |
| Bootstrap success floor (`on_bootstrap_complete`) | `_cache_dir_is_populated` | cache entry names and types | A real, non-link directory beyond the seed set and `services/`, holding at least one entry. |
| Completion verify result (`_handle_bootstrap_terminal_status`) | daemon | the verify result, and whether the bootstrap container is confirmed quiet | Verify runs only after the container is stopped or confirmed removed. A pass counts. A failure counts against the breaker. A docker outage does not. |
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
  fell back to performer-written scripts that the daemon never checked. The checklist for an empty
  manifest is `None`, as before.
- The text is persisted in the snapshot (see Compatibility), so a restart verifies the daemon's text
  and not the cache copy.

### The verify container runs only the daemon's text

`verify_env_cache_clean` runs:

    docker run --rm -i -e BASH_ENV= -e ENV= -e _DEVENV_SOURCED=1 -e _DEVENV_SKIP_SERVICES=1 \
      -v <cache>:<container path>:ro --entrypoint bash <image> -s

with the rendered `verify.sh` as stdin.

- **`BASH_ENV=` and `ENV=`** stop the image's profile for bash and sh. Bash sources `BASH_ENV`
  before it reads stdin, so this is the part that matters. Verified against the real image: with the
  defaults, a planted `/usr/bin/ruby` is on `PATH`; with these flags it is not.
- **`_DEVENV_SOURCED=1`** makes the profile a no-op if a variable is re-exported. The profile's first
  statement returns when this is set.
- **`_DEVENV_SKIP_SERVICES=1`** keeps any service start from running.
- **Inlined activation.** The rendered `verify.sh` carries the daemon's `activate.sh` text. It no
  longer sources `<cache>/activate.sh`, so a copy swapped in after the daemon's check is never read.
  The daemon still checks that the cache copy matches, and a mismatch fails the verify. That check
  now gates mounts and dispatch, not the verdict.
- **No cache path in the script.** The verify never names a cache file.

None is returned only when docker could not run the script at all.

### Completion verify runs after the bootstrap container is gone

The performer reports its terminal status while its container may still run. The clean verify
reads the cache read-only, so a live writer would race the verdict. The success path therefore:

1. `docker stop --time 5 <container>`. If that succeeds, the container has exited.
2. If the stop fails, `docker container inspect` decides. A missing container was removed by `--rm`,
   so it is quiet. A container still running is not quiet, and the verify is not run.
3. Only then `_verify_bootstrap_in_clean_context`.

A container that is still running fails the bootstrap with the reason
`env_cache.bootstrap_container_still_running`. A stop that errors fails it with
`env_cache.bootstrap_quiesce_failed`. Both are infrastructure failures (below).

After this, no writer exists for the cache, because consumers mount it read-only and the bootstrap
container is gone. Consumers are the only other mounts, and they are read-only. So the dispatch-time
match check is stable.

### Readiness checks validate content

- `_cache_dir_has_activate(cache_dir, expected_activate=...)` reads `activate.sh` through the anchored
  helper (219) and compares it with the rendered text. Missing, linked, hard-linked, FIFO, oversized
  (256 KiB) and different content all count as no toolchain.
- `_cache_dir_is_populated` requires a toolchain entry that is a real directory (not a link, file or
  FIFO), holds at least one entry, and is not a seed file (`activate.sh`, `manifest.json`, `verify.sh`)
  or `services/`. A dummy-named file, an empty directory and a link to a directory all fail.
- The populated check is a structural floor only. The daemon cannot predict installed toolchain
  contents (rbenv, nvm and deb extraction paths vary by pin). The daemon's verify run is the arbiter of
  whether the toolchain works.

### Infrastructure failures do not spend the breaker

When docker cannot run the verify (`None`), or the container cannot be confirmed quiet, the bootstrap
fails with `infra_failure=True`. `EnvCacheService.on_bootstrap_complete` then records the reason on the
cache state and logs `env_cache.bootstrap_infra_failed` with an operator hint (check the docker
daemon). It does not increment `bootstrap_attempts`, so the breaker is not spent by an outage. The
cooldown still retries it, and a real verify failure still counts. A docker outage therefore retries
every cooldown, which is the bounded rate the 077 cooldown already sets.

## Residuals (accepted, and what closes them)

These are what the daemon does not check. The PR goal is that the daemon's decisions do not run or
trust performer-controlled code or text. Each residual is outside that goal.

- **Consumer shells run cache code.** A consumer container sources the cache's `activate.sh` through
  the image profile, runs toolchain binaries from the cache, and starts `services-start.sh` (the
  profile's spec-117 start-on-activation). This is by design: consumers need the toolchain, and the
  cache is mounted read-only into them. A performer that wrote the cache can therefore run code in a
  consumer's shell. Closing this needs binary provenance (digests of the installed toolchain, or a
  snapshot the daemon builds), tracked as a separate issue.
- **Service readiness at bootstrap.** The performer decides whether required services started and
  answered (`run_service_readiness`), then reports `env_bootstrap_complete`. The daemon does not run
  `services-start.sh` or `services-health.sh`. It cannot start services in the read-only clean
  container, because the services keep their data inside the cache. A lying performer can get a cache
  marked complete with a service that is not up. Consumers start their own services through the
  profile. A test pins that the daemon ignores the health script: a rewritten health script changes
  neither the toolchain verify nor its verdict.
- **Installed toolchain provenance.** The performer installs the toolchain, so it can place a binary
  that passes a check such as `ruby -v`. The verify proves the toolchain runs, not where it came from.
- **`services/services.json` in the cache key.** The performer can change it, and that changes only
  whether a bootstrap re-runs. It does not change the spec-file SHA, so it cannot skip a re-bootstrap
  on a spec change.
- **No container id.** A transport that does not expose the bootstrap container's id cannot be
  quieted. The daemon logs `env_cache.bootstrap_container_unknown` and proceeds. The docker transport
  always provides the id.

## Compatibility

- **Schema v35.** `EnvCacheStateSnapshot` gains `rendered_verify_sh` and `rendered_activate_sh`, both
  `None` by default. Older snapshots load unchanged. Both fields keep their text byte for byte
  (`StringConstraints(strip_whitespace=False)`), because the snapshot strips whitespace by default and
  the exact `activate.sh` comparison needs the trailing newline.
- **One re-bootstrap after upgrade.** A cache bootstrapped before 218 has no rendered script and no
  matching activate record. It fails closed: consumers are held, and the existing activate-missing path
  re-bootstraps it once.
- **Empty-manifest symphonies.** These now get a daemon-owned verify.sh and activate.sh. The bootstrap
  prompt says a coordinare-provided verify.sh exists, with no checklist.
- **Verify container argv.** The docker run now carries the four `-e` flags above. Any operator tooling
  that pattern-matches the verify's docker command must allow them.

## Refusal

A refused operation is never followed. Refusals are logged as before, and the verify returns False with
a reason (`env_cache.clean_verify_refused`, `env_cache.activate_mismatch`). A refused cache dir is
reported through `env_cache.bootstrap_cache_refused` on completion.

## Tests

- `tests/unit/test_218_bootstrap_trust.py`: the first-round attacks (verify.sh rewritten, activate.sh
  rewritten, dummy-named entries, the health script, the honest end-to-end path, the snapshot round trip).
- `tests/unit/test_218_verify_container.py`: the exact docker argv, the `-e` flags, the stdin, the inlined
  activation, and a rewrite of activate.sh during the run that does not change the verdict.
- `tests/unit/test_218_completion_ordering.py`: the container is stopped before verify; a still-running
  container fails closed; a removed container counts as quiet; docker outage is an infrastructure failure
  that does not spend the breaker, and a real failure still does.
- `tests/unit/graph/nodes/test_218_dispatch_activate_gate.py`: the dispatch gate refuses a tampered
  activate.sh with the real verify, and the readiness gate's False branch refuses it without running docker.
- `tests/e2e/test_218_verify_real_image.py` (`-m e2e`, real docker, image from
  `COORDINARE_E2E_PERFORMER_IMAGE`): a planted deb with a fake ruby does not pass the verify, and 5 verifies
  with a writer swapping activate.sh continuously all fail as they should.

Tests changed because they pinned the old path (the change is to fixtures and expectations only):

- `tests/unit/test_060_env_cache.py`: the mount and persistent-collect tests write the rendered activate;
  the empty-manifest test now expects daemon scripts; the poll-loop tests stub the clean verify (3-tuple)
  and give their fake processes `returncode`/`wait`. The idle-reap test accepts exactly one stop, the
  completion quiesce, instead of none.
- `tests/unit/graph/nodes/test_dispatch_performer.py`: `_ready_env_cache` stores a matching activate, and an
  autouse fixture keeps the 093 gate in its degraded pass-through for these volume tests. The False branch
  is covered by `test_218_dispatch_activate_gate.py`, which does not use that fixture.
- `tests/unit/test_daemon_snapshot_persistence.py`, `test_219_env_cache_root_mounts.py`,
  `test_219_env_cache_safe_writes.py`, `tests/integration/test_env_cache_lifecycle.py`: rendered-script and
  activate fixtures (first round).
- `tests/unit/test_env_manifest.py`: `test_sources_activate_and_exits` becomes
  `test_inlines_activate_and_exits`, which asserts the cache path is no longer named.
- Version pins (first round, schema v35): the `CURRENT_SCHEMA_VERSION` assertions and the contract enum.

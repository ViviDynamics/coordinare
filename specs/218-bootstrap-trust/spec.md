# 218: Env bootstrap success is decided by daemon-owned scripts

## Problem

The env-cache directory is written by the bootstrap performer, which runs untrusted code.
Three daemon decisions read that directory, so anything the performer wrote there could decide
its own success. Found in the review of #217 (spec 208).

- **verify.sh.** The clean-context verify ran `verify.sh` from the cache, in a fresh container
  with the cache mounted read-only. Its exit code decided whether the bootstrap passed. The
  performer could rewrite that file to `exit 0`.
- **`_cache_dir_is_populated`.** It checked entry names only, so a dummy file named like a
  toolchain passed.
- **`_cache_dir_has_activate`.** It gated mounts and dispatch on the existence of `activate.sh`
  and nothing else. A link was refused (219), but a rewritten or emptied file passed.

## Inventory of decision points

| Decision | Site | Reads | Now |
| --- | --- | --- | --- |
| Clean-context verify (bootstrap completion, restart verify, dispatch readiness gate 093) | `verify_env_cache_clean` | `verify.sh` from the cache | The daemon's rendered text, piped on stdin to `bash -s` in the container. The cache is mounted read-only. |
| Consumer mount and dispatch hold (`get_env_volume_for_symphony`, `_collect_env_volumes_for_persistent_performer`, `_env_cache_hold`, `_env_readiness_gate` persistent check) | `_cache_dir_has_activate` | `activate.sh` from the cache | Byte-identical to `EnvCacheState.rendered_activate_sh`. |
| Phantom-success re-bootstrap (`check_and_trigger`) | `_cache_dir_has_activate` | `activate.sh` | Same exact match. |
| Bootstrap success floor (`on_bootstrap_complete`) | `_cache_dir_is_populated` | cache entry names and types | A real, non-link directory beyond the seed set and `services/`, holding at least one entry. |
| Completion verify result (`_verify_bootstrap_in_clean_context`) | daemon | verify result | Only a pass counts. None (docker could not run) fails the bootstrap. |
| Cache key input (`load_prior_manifest_bounded`) | env_cache | `services/services.json` | Unchanged. See "Still performer-controlled". |

Not a decision: `services/services-start.sh` and `services-health.sh` are run by the performer,
inside its own container (`workspace.run_service_readiness`). The daemon never executes them.

## Design

### The daemon renders and keeps the scripts

`EnvCacheService._build_manifest_artifacts` renders `verify.sh` and `activate.sh` once, from the
manifest, and stores the text on `EnvCacheState` as `rendered_verify_sh` and
`rendered_activate_sh`. The cache copies are still written, for the performer to read, but they
are never run or trusted.

- The text is set only after every write succeeds. `_do_dispatch` clears it before each render,
  so a refused or failed write leaves no script on record, and the verify fails closed.
- An empty manifest still gets the daemon-owned scripts (a verify that checks nothing). Before
  218 it fell back to performer-written scripts, which the daemon never checked. The checklist
  for an empty manifest is `None`, as before.
- The text is persisted in the snapshot (see Compatibility), so a restart verifies the daemon's
  text and not the cache copy.

### Verification runs the daemon's script

`verify_env_cache_clean` now:

1. Returns False when no daemon-rendered script is on record (a legacy snapshot or a refused
   write). Before 218 this was None (degraded), which let a performer-caused refusal pass.
2. Refuses a linked or non-directory cache dir (False).
3. Requires the cache's `activate.sh` to equal the rendered text (False, with
   `env_cache.activate_mismatch`).
4. Runs `docker run --rm -i -v <cache>:<path>:ro --entrypoint bash <image> -s`, with the rendered
   `verify.sh` text as stdin. The cache copy of `verify.sh` is never named in a command.

None is returned only when docker could not run the script at all.

### Readiness checks validate content

- `_cache_dir_has_activate(cache_dir, expected_activate=...)` reads `activate.sh` through the
  anchored helper (219) and compares it with the rendered text. Missing, linked, hard-linked,
  FIFO, oversized (256 KiB) and different content all count as no toolchain.
- `_cache_dir_is_populated` requires a toolchain entry that is a real directory (not a link,
  file or FIFO), holds at least one entry, and is not a seed file (`activate.sh`,
  `manifest.json`, `verify.sh`) or `services/`. A dummy-named file, an empty directory and a
  link to a directory all fail.
- The populated check is a structural floor only. The daemon cannot predict installed
  toolchain contents (rbenv, nvm, deb extraction paths vary by pin). The daemon's verify run,
  which is the decision the bootstrap has to pass, is the arbiter of whether the toolchain works.

### Completion fails closed

`_verify_bootstrap_in_clean_context` accepts only a passing run. A non-zero exit, a refusal and
a run that could not start all fail the bootstrap. The 088 circuit breaker bounds the retries.

## Still performer-controlled (accepted)

- **Service readiness.** The performer decides whether required services started and answered
  (`run_service_readiness`), then reports `env_bootstrap_complete`. The daemon cannot verify
  that without starting the services. It would have to run `services-start.sh` and
  `services-health.sh` in a clean container, but the cache is read-only there and the services
  keep their data inside the cache. A lying performer can therefore get a cache marked complete
  with a service that is not up. Consumers still start the services themselves. The daemon does
  not treat `services/services-health.sh` as an input, and a test pins that: a health script
  rewritten to exit 0 does not pass a failing toolchain verify, and a failing health script does
  not fail a passing one. Closing the gap needs a separate design (a writable service-data
  mount for the verify run). It is a follow-up, not part of 218.
- **Installed toolchain content.** The performer installs the toolchain, so it can place a
  binary that passes a check such as `ruby -v`. The verify proves the toolchain runs, not where
  it came from. Binary provenance (digests of installed toolchain files) is a follow-up.
- **Cache key input.** `services/services.json` in the cache is read for the inference cache
  key. The performer can change it, and that changes only whether a bootstrap re-runs. It does
  not change the spec-file SHA, so it cannot skip a re-bootstrap on a spec change.
- **Docker unavailable.** When docker cannot run at all, the restart and dispatch paths keep the
  077 degraded pass-through (None). Completion fails closed.

## Compatibility

- **Schema v35.** `EnvCacheStateSnapshot` gains `rendered_verify_sh` and `rendered_activate_sh`,
  both `None` by default. Older snapshots load unchanged. Both fields keep their text byte for
  byte (`StringConstraints(strip_whitespace=False)`), because the snapshot strips whitespace by
  default and the exact `activate.sh` comparison needs the trailing newline.
- **One re-bootstrap after upgrade.** A cache bootstrapped before 218 has no rendered script
  and no matching activate record. It fails closed: consumers are held, and the existing
  activate-missing path re-bootstraps it once.
- **Empty-manifest symphonies.** These now get a daemon-owned verify.sh and activate.sh, and the
  bootstrap prompt says a coordinare-provided verify.sh exists, with no checklist. Their
  performer-written scripts are no longer trusted.

## Refusal

A refused operation is never followed. Refusals are logged as before, and the verify returns
False with a reason (`env_cache.clean_verify_refused`, `env_cache.activate_mismatch`). A
refused cache dir is reported through `env_cache.bootstrap_cache_refused` on completion.

## Tests

`tests/unit/test_218_bootstrap_trust.py`, one attack per test, plus the honest path:

- a performer-rewritten `verify.sh` that exits 0 does not pass (the daemon's text is the stdin,
  and the cache copy is never named);
- a missing rendered script fails closed, with no docker call;
- a rewritten, emptied or sourced `activate.sh` does not gate a mount or the verify (no docker
  call); a link with matching content is still refused;
- a dummy file, an empty file, an empty directory, a services-only cache and a link named like a
  toolchain are not populated; a real toolchain directory is; a dummy file fails completion;
- a rewritten health script does not pass readiness, and does not change the toolchain decision;
- completion fails when the verify cannot run;
- the honest path end to end: the scripts are rendered and recorded, the toolchain is installed,
  the clean verify passes on the daemon's stdin, completion succeeds, and the consumer gets a
  read-only mount;
- the rendered scripts survive a snapshot round trip, and a v34 snapshot loads closed.

Tests changed because they pinned the insecure path (the honest state now includes the rendered
scripts; the change is to fixtures and to expectations only):

- `tests/unit/test_060_env_cache.py`: the mount and persistent-collect tests touched an empty
  `activate.sh` (now the rendered text). `test_no_deps_returns_none_false` expected the
  performer-written fallback (now `test_no_deps_still_renders_daemon_scripts`). The poll-loop
  tests stub the clean verify, which has its own tests here.
- `tests/unit/graph/nodes/test_dispatch_performer.py`: `_ready_env_cache` stores a matching
  `activate.sh`. An autouse fixture keeps the 093 gate in its degraded pass-through for these
  volume tests, as before 218.
- `tests/unit/test_daemon_snapshot_persistence.py`: the verify tests use a rendered script, not a
  cache copy. The "absent script" test is now "refuses without a daemon-rendered script" (False,
  not None). The completion flush test seeds a toolchain file inside its toolchain directory.
- `tests/unit/test_219_env_cache_root_mounts.py`, `test_219_env_cache_safe_writes.py`: the
  `_cache_dir_has_activate` calls pass the rendered text. The link is still refused.
- `tests/integration/test_env_cache_lifecycle.py`: the seeded toolchain holds a file, and the
  activate record is the rendered text.

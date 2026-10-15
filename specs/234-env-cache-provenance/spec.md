# 234: Env-cache provenance. Consumers run only what the daemon verified

## Problem

The env cache is written by the bootstrap performer, which runs untrusted code. Every later consumer
performer for the symphony mounts the same directory and trusts what it finds. #218 made the daemon's
own decisions run only daemon-rendered text, but consumers still executed whatever the cache held:

- **The profile (BASH_ENV).** In every consumer shell, `devenv-profile.sh` unpacked `debs/*.deb` from
  the cache, symlinked missing `/usr/bin` entries to the extracted files, sourced the cache's
  `activate.sh`, and ran `services/services-start.sh`.
- **The performer's own reads.** `workspace.py` sourced the cache's `activate.sh` for the env snapshot,
  and ran `services-start.sh` and `services-health.sh` from the cache.
- **Anything changed after verification.** The daemon verified the cache once, when the bootstrap
  finished. A writer could change any file afterwards, and the next consumer ran the change.
- **Planted binaries.** A toolchain binary in the cache runs in consumers and in the verify. A version
  check passes for a fake that prints the right version.

## Scope

The daemon freezes the cache when a bootstrap has been verified, and records a digest manifest of it.
Before each consumer dispatch it checks the cache against that manifest. A consumer runs only what the
manifest covers, and only the scripts the daemon handed it.

**What this proves.** Between a verified bootstrap and the next consumer dispatch, the cache bytes are
the bytes the daemon verified. Consumers run the rendered scripts from the daemon, the debs whose digest
is in the manifest, and the toolchain files the manifest covers.

**What this does not prove.** The toolchain binaries themselves are still what the bootstrap installed.
The digest proves they are unchanged since verification. It does not prove they are benign. A malicious
bootstrap that installs a backdoored Ruby produces a cache that passes, and that cache is frozen as good.
Benign provenance needs a trusted build, which this spec does not provide.

## Design

### 1. Freeze after a verified bootstrap

After the bootstrap container is proven gone and the clean-context verify passes, the daemon walks the
cache (`EnvCacheService.freeze_provenance`) and writes a manifest:

- **Entries.** Every entry below the cache dir: relative path, kind (`file`, `dir` or `link`), size, mode,
  inode, `ctime_ns`, and for a file its sha256. A link is recorded by its target text.
- **Links stay inside the cache.** A link must be relative, and its target must resolve to a path inside
  the cache dir. An absolute link, or one that climbs out, refuses the freeze.
- **Hard links and special files refuse.** A file with more than one link, a FIFO, a socket or a device
  refuses the freeze.
- **Scripts.** The manifest carries the rendered `activate.sh` text (EnvCacheState), and the bytes of
  `services/services-start.sh` and `services/services-health.sh` when they exist, read once at freeze.
  Consumers run these bytes, not the cache's copies.
- **Debs.** `debs/*.deb` digests, so the performer can refuse any deb that changed.

The walk uses the anchored helpers from 219 and 220 (`mounted_file.walk_mounted_tree`). Every component is
opened with `O_NOFOLLOW` relative to its directory descriptor, and a link is read with `readlink`, never
followed. The walk runs in a thread.

### 2. Bounds (named constants in `services/env_provenance.py`)

| Constant | Value | Meaning |
| --- | --- | --- |
| `PROVENANCE_MAX_ENTRIES` | 100 000 | entries (files, dirs and links) |
| `PROVENANCE_MAX_BYTES` | 2 GiB | total file bytes hashed |
| `PROVENANCE_MAX_SCRIPT_BYTES` | 256 KiB | each script carried in the manifest |
| `PROVENANCE_MAX_DEB_BYTES` | 512 MiB | one deb copied to a consumer |
| `PROVENANCE_FULL_RECHECK_S` | 3600 s | the periodic full re-hash |

A cache over a bound is **refused** with its reason (`env_cache.provenance_refused`). The bootstrap fails
on the verdict budget with the reason "cache exceeds ...", and the operator sees it. The daemon never
skips the check for a large cache.

### 3. Where the manifest lives

A daemon-owned file, `<state directory>/env-provenance/<sanitised>.json`, written with `replace_mounted_file`
and refused if its directory lies inside a performer mount. The snapshot keeps only the manifest's sha256
(`provenance_digest`) and the freeze time. Before every use the file must hash to the recorded digest. A
missing or altered file means no frozen provenance, which holds dispatch and re-bootstraps.

The per-file list stays out of the snapshot: a 100 000 entry list would bloat every save.

### 4. Verification cost

Each consumer dispatch runs `check_cache`:

- **Fast path (every dispatch).** A stat-only walk (no hashing) compares each entry with the manifest by kind,
  mode, size, inode and `ctime_ns`, and each link by its target text. `ctime_ns` is the field that matters:
  a process cannot set it, so a rewrite that restores the size and the mtime still changes it. The walk over
  100 000 entries measured 0.4 s on the development Mac (warm cache, 100 000 small files, one 1 000-directory tree).
  A full hash pass over the same tree measured 2.9 s. Both scale with the entry count and the bytes; a deployment
  with a slower disk should re-measure before relying on the per-dispatch cost.
- **Any mismatch, or any added or removed entry, means a full re-hash.** Every file is hashed and compared
  with its sha256. A changed hash is drift. A stat mismatch with the same hashes (a touch) passes, and the
  cache is not re-recorded.
- **Periodic full re-hash.** `PROVENANCE_FULL_RECHECK_S` after the last one, and on the first dispatch for a
  symphony after a daemon restart. A writer who forges `ctime_ns` within the filesystem's timestamp
  granularity is caught here, within the hour.

The residual window is the time between two full re-hashes, for a writer who can both change bytes and
restore `ctime_ns` inside the timestamp granularity. The bound is one hour. A full re-hash reads up to 2 GiB.

### 5. Verify before every consumer dispatch

`dispatch_performer._env_provenance_gate` runs for every consumer dispatch that would mount the cache, after
the existing hold and before the readiness gate and the mount. A failed check (drift, a refused walk, a missing
or altered manifest, no frozen provenance) refuses dispatch:

- It logs `dispatch_performer.env_provenance_refused` with the reason, and writes an activity entry
  (`env_provenance_refused`) so the feed explains the wait.
- It releases the slot, and calls `mark_runtime_health_failed`, the existing re-bootstrap seam. The next cycle
  re-bootstraps the current spec. The bootstrap budget bounds the loop.

A drifted cache is never dispatched into.

### 6. Consumers receive the rendered steps, not the cache's scripts

The job payload carries an `env_provenance` block (new field, `JobInitPayload.env_provenance`):

    slug, cache_path, activate_sh, services_start_sh, services_health_sh, debs {relpath: sha256}, digest

The daemon builds it from the manifest, not the cache. The performer, before the job starts:

1. Creates `/var/lib/devenv-provenance/<slug>/` (container-local, not mounted).
2. Writes `activate.sh` and `services/services-{start,health}.sh` from the payload text.
3. Copies each deb listed in `debs`. It reads the bytes from the cache once, checks the sha256 of those bytes,
   and writes them to `debs/` only on a match. There is no hash-then-reread, so no time-of-check gap. A deb that
   is not in the list is never read, and a mismatch refuses the job.
4. Exports `_DEVENV_PROVENANCE_DIR=/var/lib/devenv-provenance`.

Every performer read of the scripts goes to the provenance directory (`workspace._frozen_scripts_dir`): the env
snapshot, `services-start.sh`, `services-health.sh`. The profile loops over the provenance directory only.

### 7. The profile is restricted

`devenv-profile.sh` chooses its root:

- `_DEVENV_PROVENANCE_DIR` set (consumer with frozen provenance): the root is the provenance directory. The
  cache's `activate.sh`, `services/` and `debs/` are never read.
- `_DEVENV_BOOTSTRAP=1` (the env_bootstrap job, which is the cache writer): the root is the cache root, as before.
  The bootstrap writes the cache, so it reads its own output.
- Neither: the profile activates nothing from any cache. A consumer without provenance gets no toolchain from the
  profile. The performer refuses such a job (see 8).

### 8. Image compatibility: refuse, never fall back

- **Capability.** The performer reports `env_provenance_version` on `GET /status` (the new version is 1). An older
  image does not send it, so the daemon reads 0.
- **Refusal.** Before the job is posted, if the payload carries `env_provenance` and the performer reports
  version 0, the daemon refuses dispatch with the reason **"performer image too old for env-cache provenance
  (#234)"**. The ephemeral path checks the status it already read in `wait_ready`. The persistent path checks
  `get_status()`. The container is refused, not run.
- **No silent fallback.** An old image never runs the cache's scripts. The new performer refuses a job that names
  an `env_cache_path` with no `env_provenance` (reason "env cache without provenance (#234)").
- **Deploy.** The performer image must be rebuilt, because `devenv-profile.sh` and the performer change. Until it is,
  consumer dispatch for a symphony with an env cache refuses with the reason above. Bootstrap jobs still run.
  `Dockerfile.full` is unchanged: it already sets `BASH_ENV` to the profile, and the profile is copied in.

### 9. Schema

Schema v37. `EnvCacheStateSnapshot` gains `provenance_digest: str | None` and `provenance_frozen_at: datetime | None`,
both `None` by default. v36 snapshots load with `None`, so there is no frozen provenance: the first consumer dispatch
is refused and re-bootstraps once. That is the upgrade cost.

## Log vocabulary

- `env_cache.provenance_frozen` (symphony, entries, bytes, digest), `env_cache.provenance_refused` (reason)
- `env_cache.provenance_check_failed` (symphony, reason, full), `env_cache.provenance_recheck_full`
- `dispatch_performer.env_provenance_refused` (symphony, card, reason)
- `http_performer.env_provenance_refused` (the image cannot take a frozen payload: the reason is the #234 refusal)
- performer: `env_cache.provenance_materialised` (on success), `env_cache.provenance_refused` (a deb changed, or a
  step could not be read: the job is refused with the reason), `env_cache.provenance_missing` (a consumer with a
  cache path and no frozen payload)

## Out of scope

- **Toolchain provenance.** As above: the digest proves unchanged, not benign.
- **Consumers already running during a re-bootstrap.** A consumer already running when a re-bootstrap starts reads
  the cache the bootstrap is writing. The gate stops new dispatch, not a running container. Closing this needs a
  copy-on-verify snapshot, which is option 2 of the issue and was not chosen.
- **Persistent performers.** They never receive the cache mount (`_resolve_env_volumes`). A persistent performer
  started with an operator-configured volume onto the cache is outside the gate.
- **Subprocess transport.** It runs on the host and never mounts a cache.

## Tests

Attacks, one failing test each:

- a file changed after verification refuses dispatch (`tests/unit/test_234_attacks.py`)
- a planted deb not in the manifest is never unpacked (`tests/unit/test_234_attacks.py`, and the performer materialise test)
- a swapped `activate.sh` is not sourced by consumers (`tests/unit/test_234_profile.py`, a shell test)
- a services-start script added to the cache is not run (`tests/unit/test_234_profile.py`)
- a link planted after verification is refused (`tests/unit/test_234_attacks.py`)
- the cache bound is enforced (`tests/unit/test_234_freeze.py`)

The honest path runs end to end with fakes (`tests/unit/test_234_honest_path.py`). The real-image lane
(`tests/integration/test_234_real_image.py`, marker `docker_image`) reproduces a post-verification swap in the
built image and shows the consumer ignores it or refuses.

Existing tests updated because they pinned the old profile: see the PR body for the list.

## Quality gate

Changed protected path: `.github/workflows/pr-ci.yml` (the Docker Images step also runs the 234 real-image test
in the 218 lane). No Dockerfile or `pyproject.toml` change. The PR body carries the "### Quality gate changes"
paragraph.

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
- **Links stay inside the cache.** A link must be relative. Its target is resolved component by component from the
  link's own directory, and a component that names another link in the cache is followed through that link's
  target. A resolution that reaches above the cache root refuses the freeze, and so does a chain longer than 40 hops
  (a loop). A lexical `..` check is not enough: `a/q/L -> ../x/y/S/../../../..` with `a/x/y/S -> ../../..` climbs out
  only through the second link. Absolute targets refuse outright.
- **Hard links and special files refuse.** A file with more than one link, a FIFO, a socket or a device
  refuses the freeze.
- **Scripts.** The manifest carries the rendered `activate.sh` text (EnvCacheState), and the bytes of
  `services/services-start.sh` and `services/services-health.sh` when they exist, read once at freeze.
  Consumers run these bytes, not the cache's copies.
- **Debs.** `debs/*.deb` digests, so the performer can refuse any deb that changed.

The walk uses the anchored helpers from 219 and 220 (`mounted_file.walk_mounted_tree`). Every component is
opened with `O_NOFOLLOW` relative to its directory descriptor, and a link is read with `readlink`, never
followed. The walk runs in a thread.

### 2. Bounds

The two walk bounds are config knobs on the env-cache section (`env_cache.provenance_max_entries`,
`env_cache.provenance_max_bytes`), validated at load (entries 1 000 to 10 000 000; bytes 1 MiB to 256 GiB).
The defaults are 1 000 000 entries and 16 GiB. They are sized from measurements: a Node install is about 40k
entries and 470 MiB, and an rbenv about 480k entries and 7.7 GB. Each walk takes its bounds as a
`ProvenanceLimits` value, and the service reads them from config.

| Constant (`services/env_provenance.py`) | Value | Meaning |
| --- | --- | --- |
| `DEFAULT_LIMITS` | 1 000 000 entries, 16 GiB | the config defaults |
| `PROVENANCE_MAX_SCRIPT_BYTES` | 256 KiB | each script carried in the manifest |
| `PROVENANCE_MAX_MANIFEST_BYTES` | 256 MiB | the saved manifest file |
| `PROVENANCE_FULL_RECHECK_S` | 3600 s | the periodic full re-hash |
| `PROVENANCE_SLUG_MAX` | 120 | the performer-side directory name (see the slug rule in section 6) |

A cache over a bound is **refused** with its reason, and the reason names the knob to raise, for example
"cache has more than 1000000 entries; raise env_cache.provenance_max_entries if this cache is legitimately this
large". The bootstrap fails on the verdict budget with that reason, and the operator sees it. The daemon never
skips the check for a large cache.

### 3. Where the manifest lives

A daemon-owned file, `<state directory>/env-provenance/<sanitised>.json`, written with `replace_mounted_file`
and refused if its directory lies inside a performer mount. The snapshot keeps only the manifest's sha256
(`provenance_digest`) and the freeze time. Before every use the file must hash to the recorded digest. A
missing or altered file means no frozen provenance, which holds dispatch and re-bootstraps.

The per-file list stays out of the snapshot: a million-entry list would bloat every save.

**The file's size is bounded by the entry bound (second review).** The saved form is one JSON array per entry, and each
entry may carry a path of up to `PROVENANCE_MAX_PATH_BYTES` (512). The per-entry budget is that path plus 256 bytes of
fixed JSON overhead, so the cap is `manifest_cap_bytes(max_entries)` = `max_entries * 768`. At the default bound that is
about 768 MB. The reviewer measured 258 to 297 MiB for a million entries with 70 and 110 character paths, so a cache at
the default bound fits. A cache with a longer path is refused at freeze with the path length. A manifest over its cap
names `env_cache.provenance_max_entries`.

**Format and load cost.** Each row is a named tuple, not a validated model, and the file is version 2 of the format. The
first attempt used a pydantic model per entry and held about 2.7 GB resident for a 1M-entry manifest. The named-tuple
form holds about 1.5 GB resident and loads in about 2.1 s. The load runs in `asyncio.to_thread`, off the event loop.
Measured on this machine (`load_manifest`, 1 000 000 entries, 222 MiB file): load and validate 2.1 s, resident after
load 1485 MiB, from 49 MiB before. A deployment that holds several million-entry caches holds that much per symphony. The
knob is the lever: lower `provenance_max_entries` to the size the host can carry.

### 4. Verification cost (third review: nothing per entry is held in memory)

The daemon holds no manifest rows, and no stat table, for the life of the process. What it keeps per symphony is
the manifest digest, the stat digest (two hex strings), the payload (small), and the full-pass clock. The
manifest file lives on disk.

- **Stat digest.** The stat digest is one sha256 over the records in walk order, streamed, so the memory is O(1).
  Each record is seven NUL-terminated fields: path, type, mode, size, inode, ctime_ns, and link target. A POSIX name and
  a link target cannot contain NUL, and every record has the same field count, so the byte stream parses back into
  exactly these records. The walk is deterministic (each directory's names are sorted, depth first), so the order needs
  no sort and no list. Reordering, swapping or dropping a record changes the digest. The review asked for an additive
  hash to be replaced: the earlier additive sum (mod 2**256) was weak against generalised-birthday attacks, and it is
  gone. The test `test_234_review4.py` pins the order and field-boundary behaviour.
- **Freshness and coalescing (review 4).** Concurrent dispatches for one symphony share one stat walk in flight, so N
  dispatches cost one walk. A passing stat check is reused for `env_cache.provenance_stat_fresh_s` seconds (default 30,
  validated to 0 through 3600; 0 disables the reuse), so a dispatch inside the window causes no walk. The window is the
  detection latency for changes that do not move the stat digest: a change made within the window is seen by the next
  walk after it, and the hourly content check bounds the rest.
- **Dispatch (every consumer dispatch).** The stat walk runs in a thread, and its digest is compared with the stat
  digest recorded at the last verified freeze or content check (`<sanitised>.stat.json`, bound to the manifest
  digest). A match is a pass, with no content read. A mismatch, or a missing record, falls to the content check.
- **Content check (mismatch, first dispatch after a restart, and the hourly pass).** The manifest is streamed
  beside the walk, and the two are compared entry by entry in walk order, content included (`verify_content`).
  Rows are read one at a time, and the file's digest is checked as they are read, so a manifest that changes
  mid-read fails. If the content matches, the new stat digest is written as the baseline, which is the ctime-churn
  case. If it does not match, it is drift, and every dispatch refuses until a new freeze.
- **Hourly full pass.** It runs in a background task (`schedule_full_pass`, from the poll cycle). It runs the same
  content check, so it costs the same as the post-restart pass.

**Measured at 990 996 entries** (a real tree, 990 directories of 1 000 small files, on the development Mac, warm
cache; `/tmp` measurement scripts, not committed):

| Step | Time | Resident after |
| --- | --- | --- |
| baseline, process start | | 50 MiB |
| freeze (streamed to disk; manifest file 145.5 MiB) | 93.5 s | 200 MiB |
| first dispatch (content check, after the freeze) | 94.7 s | 202 MiB |
| dispatch stat pass, end to end (3 runs) | 19.1 to 19.7 s | 202 MiB |
| stat digest walk alone | 19.6 s | |
| mismatch (one chmod), full content check | 112.7 s | 202 MiB |
| dispatch after the refresh (stat only) | 17.9 s | 202 MiB |

The stat pass is about 19 us per entry, which is above the "couple of seconds" the review asked about. The cost is
Python per entry (a stat call, a record string, a sha256), not the disk, and it is reported here rather than
optimised. A cache at the default bound costs about 19 s per dispatch on this machine, so an operator who wants
faster dispatch lowers `env_cache.provenance_max_entries`, or moves the stat pass to a native walker. The first
dispatch after a freeze or restart waits for the full content check, about 95 s at this size. Resident memory stays
flat (about 200 MiB against a 50 MiB baseline) as the entry count grows, which is the property the review asked for.

### 5. Verify before every consumer dispatch

`dispatch_performer._env_provenance_gate` runs for every consumer dispatch that would mount the cache, after
the existing hold and the 093 readiness gate, immediately before the mount is resolved. The check and the consumer
payload come from one read of the digest and the same manifest (`EnvCacheService.resolve_provenance`), so a
regeneration that starts mid-dispatch cannot pair a verified check with a different payload. A digest that changes
during the check refuses the dispatch. A failed check (drift, a refused walk, a missing
or altered manifest, no frozen provenance) refuses dispatch:

- It logs `dispatch_performer.env_provenance_refused` with the reason, and writes an activity entry
  (`env_provenance_refused`) so the feed explains the wait.
- It releases the slot, and calls `mark_runtime_health_failed`, the existing re-bootstrap seam. The next cycle
  re-bootstraps the current spec. The bootstrap budget bounds the loop.

A drifted cache is never dispatched into.

### 6. Consumers receive the rendered steps, not the cache's scripts

The job payload carries an `env_provenance` block (new field, `JobInitPayload.env_provenance`):

    slug, cache_path, activate_sh, services_start_sh, services_health_sh, debs {relpath: sha256}, digest

**What the payload contains (third review).** Those seven keys and nothing else. The manifest rows are never sent.
At 990 996 entries the payload measured 275 bytes (no debs in that tree). A real cache adds about 80 bytes per deb
and the rendered scripts, which are capped at 256 KiB each. The performer materialises the scripts and the listed
debs, and the digest is recorded for the audit trail. The daemon keeps one payload per symphony and digest, so the
payload cache is small.

**The slug rule (review round).** The slug is `provenance_slug(cache directory name)`. A name that already matches
`^[a-z0-9_][a-z0-9_-]*$` and is at most 120 characters is kept as it is, so an underscore-led symphony such as
`_scratch-pad-<sha6>` works. Any other name is cleaned to the pattern, truncated to fit, and suffixed with the first
8 hex characters of the sha256 of the original name. The daemon and the performer each implement this function, and
a parity test pins both to the same answers. The performer model accepts any slug the function can produce and
refuses a traversal such as `../x`. The performer also refuses a payload whose slug is not the derived slug of its
cache path. `sanitise_symphony_name` (the cache directory name) is unchanged, so existing caches keep their paths.

The daemon builds the payload from the manifest, not the cache. The performer, before the job starts:

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

## Residuals (second review)

- **The bootstrap exemption is by role.** `_require_provenance_capability` exempts a job whose role is `env_bootstrap`
  from the cache-mount-without-payload refusal, and the performer runs such a job with the cache root. A card or an
  operator lifecycle listing `env_bootstrap` gets that exemption. A daemon-only flag would close it, and was not done in
  this round. Until then, the role is the only thing that separates the cache writer from a consumer.
- **The restart full pass is inline.** The first dispatch after a restart waits for the full content check, about 95 s
  at 990 996 entries on this machine (section 4). This is the price of not trusting the recorded stat digest across a
  restart.
- **Event-loop lag (review 4).** Measured at 991 000 entries with a 50 ms sleeper: a thread walk gives p99 overshoot of
  about 2 ms; a worker process gives about 2 ms too, so the walk does not starve the loop and the walk stays in a thread.
  Method: the stat walk ran in `asyncio.to_thread` while a coroutine slept 50 ms and recorded how late it woke.
  Measured on this Mac, warm cache: 20.7 s per walk, lag p50 1.7 ms and p99 2.1 ms (threads, two runs), against
  p50 2.1 ms and p99 2.1 ms in a worker process. Resident memory stayed at 51 MiB across the walks, so the walk does not
  hold memory. The 150 MiB of resident growth after a freeze is not explained by the walk; it is a freeze-time transient
  not traced further, and it does not grow with a repeated walk.
- **Residency.** No manifest is held in memory. Resident memory does not grow with the entry count (measured in
  section 4). The rows are read from disk when a content check runs.

## Out of scope

- **Toolchain provenance.** As above: the digest proves unchanged, not benign.
- **Consumers already running during a re-bootstrap.** A consumer already running when a re-bootstrap starts reads
  the cache the bootstrap is writing. The gate stops new dispatch, not a running container. Closing this needs a
  copy-on-verify snapshot, which is option 2 of the issue and was not chosen.
- **Persistent performers.** They never receive the cache mount (`_resolve_env_volumes`). A persistent performer
  started with an operator-configured volume onto the cache is outside the gate.
- **Subprocess transport.** It runs on the host and never mounts a cache.
- **Stat soundness depends on read-only mounts and a quiesced writer.** The stat pass trusts that nothing writes the
  cache between a dispatch's check and the consumer's run, and that `ctime` is kernel-set. Read-only consumer mounts
  and the rule that no bootstrap runs while consumers are dispatched keep that true. The hourly full pass bounds how
  long a violation can go unseen. Detection latency is therefore up to one hour for a writer that can rewrite bytes and
  restore `ctime` inside the timestamp granularity.
- **Regeneration rewrites the live cache under running consumers.** A forced or drift-triggered regeneration writes the
  cache that consumers already mounted read-only. Running consumers see the new bytes. The gate stops new dispatch
  and the bootstrap holds the cache, but a consumer already running is not stopped. This is the same residual as the
  re-bootstrap item above.
- **A planted binary that passes verification is out of scope.** The verify executes toolchain binaries, so a binary
  that prints the expected version passes it, and the digest then freezes that binary as good. The digest proves
  unchanged since verification. Benign provenance needs a trusted build, which this spec does not provide.

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

Review round (`tests/unit/test_234_review_*.py`): the version gate and the dispatch wiring with the real gate
(`test_234_review_gate.py`), link containment through the reviewer's counterexample and a hop bound
(`test_234_review_links.py`), slug parity between the daemon and the performer and the long and underscore-led names
(`test_234_review_slug.py`), the knobs and the refusal that names them (`test_234_review_bounds.py`), and the
stat baseline and the payload memo (`test_234_review_stat.py`).

## Quality gate

Changed protected path: `.github/workflows/pr-ci.yml` (the Docker Images step also runs the 234 real-image test
in the 218 lane). No Dockerfile or `pyproject.toml` change. The PR body carries the "### Quality gate changes"
paragraph.

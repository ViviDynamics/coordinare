# 219: Env-cache writes stay inside the env-cache root

## Problem

The env-cache root (`env_cache_root`) is mounted read-write into performers, one cache dir
per symphony. A performer can plant a symlink, a hard link, a FIFO or a wrong-type entry
anywhere under its cache dir. The daemon wrote into that tree with plain path calls, so a
planted link at a file, at `services/` or at the cache dir itself made the daemon overwrite
a host file and chmod it 0755. Found in the review of #217 (spec 208).

## Inventory

### Writes (daemon side)

| Site | What it writes | Now |
| --- | --- | --- |
| `env_cache.write_service_scripts` | `services/` directory, `services-{start,stop,health}.sh` (0755) | `anchored_mkdir` for the directory, `replace_mounted_file(mode=0o755)` for each script |
| `EnvCacheService.initialise` | the cache dir for each symphony | `anchored_mkdir`; a refusal skips the symphony, as an I/O error did |
| `EnvCacheService._build_manifest_artifacts` | cache dir, `verify.sh` and `activate.sh` (0755), `manifest.json` | `anchored_mkdir` and `replace_mounted_file`; `verify.sh` and `activate.sh` set mode 0755, `manifest.json` keeps an existing file's mode |

Not daemon side: the service-inference writers (`services.json`, the scripts,
`cache_manifest.txt`, `.rejected`) run in the performer container (`agent/performer`). The
daemon reads `services.json` through the anchored reader from spec 208. No env-cache unlink,
rmtree, copy or chmod-by-path remains on the daemon side.

### Reads that feed a decision

| Site | Decision | Now |
| --- | --- | --- |
| `_cache_dir_has_activate` | consumer mount and dispatch gate | `activate.sh` read through the anchored walk. A link is not a toolchain. This reverses the 208 "reviewed, not changed" note, because the answer now gates a mount. |
| `_cache_dir_is_populated` | bootstrap success verdict | entry names listed through the anchored walk |
| `clean_verify` gate | whether `verify.sh` runs | `verify.sh` read through the anchored walk |
| `get_env_volume_for_symphony` | host path for the `-v` mount (rw for bootstrap) | a linked cache dir is never offered as a mount |

## Design

Four helpers in `services/mounted_file.py`, on the 208 walk (outside every mount, links are
followed; inside a mount, every component is opened with `O_NOFOLLOW` by descriptor):

- `anchored_mkdir(path, mounts=...)`: `mkdir -p` that refuses a link, regular file or FIFO at
  any component.
- `replace_mounted_file(path, data, mounts=..., mode=None)`: temp file created `O_EXCL` by
  descriptor, mode set with `fchmod` on that descriptor before the rename, then renamed over
  the leaf by directory descriptor. `mode=None` keeps an existing file's mode (0644 for a new
  file), as before.
- `mounted_dir_refusal(path, mounts=...)` and `read_mounted_dir_names(path, mounts=...)`:
  directory checks and listings through the same walk.

Every env-cache call passes the full performer mount list (`performer_mount_hosts(config)`:
the log dir, each endpoint volume, and the root). The root is also added as the cache dir's
parent. So when the root itself sits under a performer volume, the walk starts at that volume,
and a link the performer plants at the root is refused, not followed to a host directory.

Config load also rejects an `env_cache_root` that is equal to, inside, or containing the
performer log dir or any endpoint volume (resolved paths, the error names both). The check
found no overlap in the shipped examples (`~/.junie`, `~/.opencode`, `/var/log/...`).

## Review round

- **Block (root under a volume)**: the mount list above, plus the load-time rule.
- **Inherited mode**: with no explicit mode, an existing file's mode is masked to `0o755`, so
  a planted setuid, setgid, sticky or group/other-write bit is not carried over.
- **Diagnostic**: a refused cache dir after a "successful" bootstrap fails with the refusal
  reason (`env_cache.bootstrap_cache_refused`), not "installed in the wrong location".
- **`test_env` host read**: a `host_path` inside a performer mount is read through the
  anchored reader (no links below the outermost mount, 1 MiB bound). The `repo_path` source
  is still read through the clone path (not changed here).

## Compatibility

- **Config that now fails to load.** A `performer_log_dir` or an endpoint volume at `~`, or at
  `~/.coordinare`, or any directory that contains the env-cache root, is refused at load. The
  root and the volume must not overlap, compared case-insensitively where the filesystem folds
  case. Move `env_cache_root` outside every performer mount, or narrow the volume.
- **Global save.** `PUT /api/config/global` validates the whole candidate file before it writes.
  A save that would leave the file unloadable returns 422 with the reason and writes nothing.
  Before this change the same save was written and then rejected at reload.
- **Inherited mode loses group write.** A file replaced without an explicit mode (the
  routing config, the env-cache `manifest.json`) keeps its permission bits masked to `0755`.
  A group-writable file comes back `0644`. Operators who need group write must set it on the
  replacement after the write.

## Refusal

A refused operation is never followed. It logs a structlog error (`mounted_file.refused`
with the path and reason, and a call-site event: `env_cache.write_refused`,
`env_cache.mkdir_refused` or `env_cache.mount_refused`), then fails the env-cache step the way
an I/O error does: `initialise` skips the symphony, `write_service_scripts` returns `False`,
and the manifest writer returns `(checklist, False, False)`. The host file is not changed in
content or mode.

## Modes

Unchanged for every existing file in practice: the scripts are 0755, and an existing
`manifest.json` keeps its permission bits, masked to 0755 (see the review round). One deliberate difference: a new `manifest.json` is created 0644 instead of
`0666 & ~umask`. The two are equal under the usual umask 022.

## Re-review, round three

- **Case-variant overlap**: the load check uses the case-aware comparison from
  `run_transcript._within`, so `WS/caches` against a volume `ws` is refused where the
  filesystem folds case. The test branches on a runtime probe of the filesystem, not a skip.
- **Candidate global save**: `_validate_global_config` runs `validate_raw_config` on the full
  file, so an overlap is refused with 422 before the write.
- **Mount list**: `loaded_performer_mounts` is the one way to get the list. It refuses with a
  `RuntimeError` when there is no loaded config, in the daemon and in dispatch alike.
  `performer_mount_hosts` expands `~` the way the validator does.

## Re-review, final narrow check

- **NFC and NFD**: every compared path component is NFC-normalised before any casefold, in the
  overlap rule, the runtime walk and the shared `_within`. APFS treats the two spellings as one
  name. I did not add an `os.path.samestat` comparison of ancestors; the normalised comparison
  closes the reported bypass.
- **Validate what loads**: every write path (`PUT /api/config/global`, `save_section`, the
  catalog writers) validates the written text after `os.path.expandvars`, the same expansion as
  `ProjectConfiguration.from_yaml` (`config.expand_config_text`). The literal `${VAR}` text is
  still what gets written.
- **Case probe fails closed**: when the probe cannot tell (the filesystem root, a name with no
  letters, an error), the filesystem is treated as case-insensitive.
- **Global save re-checks its guard** just before the write, as `save_section` already did.

## Tests
`tests/unit/test_219_env_cache_safe_writes.py`: a link at each written file, at `services/`
and at the cache dir (also at `initialise`); a hard link at a target; a FIFO at a target (run
on a bounded thread, so a regression that blocks fails instead of hanging); a regular file
where a directory is expected; the read-side gates; the happy path (bytes, modes, existing
mode kept). The attack tests fail on the pre-219 `env_cache.py`.

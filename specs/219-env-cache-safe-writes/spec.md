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

## Tests

`tests/unit/test_219_env_cache_safe_writes.py`: a link at each written file, at `services/`
and at the cache dir (also at `initialise`); a hard link at a target; a FIFO at a target (run
on a bounded thread, so a regression that blocks fails instead of hanging); a regular file
where a directory is expected; the read-side gates; the happy path (bytes, modes, existing
mode kept). The attack tests fail on the pre-219 `env_cache.py`.

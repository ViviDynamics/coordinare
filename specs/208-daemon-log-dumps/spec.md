# 208: Daemon writes stay out of performer-writable directories

## Problem

`performer_log_dir` is bind-mounted read-write at `/var/log/performer` into every
ephemeral performer. Anything the daemon writes there can be replaced, linked or
pre-created by the container whose output it records. Run transcripts (129) and full
run logs (132) already keep their files in a daemon-only root with link and hard-link
refusal. Three older writers did not.

## Inventory

### Writes

| Site | What it writes | Where | Decision |
| --- | --- | --- | --- |
| `performer_lifecycle._dump_container_logs` (called from `stop(host_log_dir=...)`) | `docker logs` output, `<performer_id>.<cid12>.container.log`, unredacted, 5 MB tail | `performer_log_dir` | **Dropped.** The full redacted run log (132) covers it for every normal teardown. |
| `kubernetes_runtime._dump_pod_logs` (called from `stop(host_log_dir=...)`) | Pod log, `<performer_id>.log`, unredacted, no size cap | `performer_log_dir` | **Dropped.** Same reason as the Docker dump. |
| `attempt_log.AttemptLog._append` (141) | `attempts/YYYY-MM-DD.jsonl` attempt telemetry rows, opened with `open(path, "a")` (follows links) | `performer_log_dir/attempts/` when that is set | **Moved** to the daemon-only default `logs/attempts` beside the config (state fallback), appended with `O_NOFOLLOW`, mode 0600, regular single-link check. |

### Reads

| Site | What it reads | Decision |
| --- | --- | --- |
| `attempt_log.AttemptLog.reopen_attempt` | `last_attempt_log_path` from the persisted state | A persisted path under a performer-writable directory (`performer_log_dir`, passed as `untrusted_dirs`) is not reused, so a pre-208 path is replaced by today's file in the daemon-only dir. Other persisted paths keep the existing A-008 behaviour. |
| `routing_config_service` (`read_routing`, `routing_available`, `_read_entries`, the create/update/delete writers) | The routing YAML at a path an endpoint mounts (`SELFHOSTED_ROUTING_CONFIG`). Operator-declared; `ro` in every shipped example, but `rw` is allowed. | Reads go through `mounted_file.read_mounted_file` (no links, regular single-link file, size bound). Writers refuse any location that is not a plain file, so a read-modify-write never runs on planted content. Entries shown on the dashboard are redacted. |

### Reviewed, not changed

- `env_cache._cache_dir_is_populated` and `_cache_dir_has_activate` (the env cache root is
  mounted read-write during bootstrap). They read entry names and existence only, never
  content, so a planted link changes a boolean and nothing else.
- `persona_classifier._read_project_context` and `test_env_loader.load_test_env` read
  `CLAUDE.md`, `AGENTS.md` and the test-env file from a symphony clone. The clone is not
  a performer mount in the examples, but a performer that commits a symlink can still
  make the daemon read through it. **Follow-up**: route these reads through
  `read_mounted_file` as well.
- Transcript and run-log reads already refuse links (129, 132).

## Behaviour after this spec

- `performer_log_dir` still exists and is still mounted at `/var/log/performer`. Performers
  (Hermes, and any backend that honours `PERFORMER_LOG_DIR`) still write their own
  artifacts there. The daemon no longer writes anything into it.
- A container that is removed without a run log (`run_logs.enabled: false`, a collector
  that could not be built, a saturated run-log pool, or a shutdown, which skips log
  collection by design) has no daemon-side copy of its output after this change. Before,
  the Docker dump, and the Kubernetes dump, covered those cases unredacted. This is a
  known gap, accepted because it avoids storing a second, unredacted copy. Reinstating
  it would mean a daemon-only dump under the state directory through the 132 writer.
- Attempt telemetry moves. Rows already in `<performer_log_dir>/attempts/` are not
  copied. Move them by hand if they are wanted. The new location is the existing
  default (`logs/attempts`, already in `.gitignore`).

## Log vocabulary

- `mounted_file.refused` (`path`, `reason`): a read from a performer-writable path was
  refused (link, hard link, not a regular file, too large, open failed).
- `attempt_log.write_failed` is unchanged.
- `performer_lifecycle.log_dump_*` and `kubernetes_runtime.log_write_failed` are removed.

## Config and docs

- `config.example.hermes.yaml` and `config.example.yaml` describe the daemon dump and the
  attempts location; both updated.
- `config.py` field comment for `performer_log_dir` updated.

## Review round 2

Decided by the reviewer: the shorter stop is accepted (no `docker logs` or pod-log dump
before stop). Shutdown and runs without a run log keep no daemon-side copy. This is the
stated gap above, and it stays.

Changes made in response to the review:

1. **Links above the leaf.** Every performer-writable read, write and hash walks the
   directory chain with `O_NOFOLLOW` per component, and opens the leaf relative to the last
   directory descriptor. Anchor: the operator's root where one is known (env-cache root,
   clone), so the root itself may be a link. Without a root, the whole parent must resolve
   to itself, or the read is refused. Routing writes go through `replace_mounted_file`
   (temporary file `O_EXCL`, rename by directory descriptor, mode preserved), not
   `atomic_write_yaml`.
2. **Prior manifest.** `env_cache.load_prior_manifest_bounded` reads
   `<cache dir>/services/services.json` with the bounded reader (1 MiB, anchored at the env
   cache root) and runs on a worker thread. The package keeps `parse_prior_manifest`. The
   performer does not use `load_prior_manifest`.
3. **Dashboard hash.** `routing_content_hash` replaces `compute_content_hash` for the routing
   table in `/api/config/all`. A refused table has no hash and reads as the read-only state.
   The write-side baseline uses the same reader; a refused table returns `forbidden`.
4. **Project context.** `CLAUDE.md` and `AGENTS.md` from the clone are read through the
   bounded reader anchored at the clone (256 KiB). A link or oversize file reads as absent.
5. **Attempt directories** are created 0700 at every level the daemon creates
   (`ensure_private_dir`).
6. **FIFO at the attempt file**: the append opens `O_NONBLOCK`, so a FIFO fails with ENXIO
   and is logged, not waited on.
7. **Reopen**: the untrusted directory and the persisted path are both resolved before
   comparison.
8. **Mounts**: an attempt directory inside any performer mount (performer log dir, endpoint
   volume, env-cache root) falls back to the state directory with `attempt_log.dir_in_performer_mount`.
   If the state directory is itself inside a mount, `attempt_log.state_dir_in_performer_mount`
   is logged as an error.
9. **Docs**: `config.example.yaml` (Attempt telemetry block) and
   `docs/operators/containerized-performers.md` (Where logs and telemetry go). Existing rows are
   not migrated.
10. **Test fake**: `test_132_run_log_teardown.py` stop signature matches the runtime.

Out of scope, reported to the coordinator: the bootstrap trust issue (`verify.sh` and
populated-cache checks rely on performer-writable files).

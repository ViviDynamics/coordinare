# 220: Mounted-file follow-ups from the reviews of #208 and #219

## Threat model

Anything mounted writable into an untrusted performer container (`performer_mount_hosts(config)`)
can be renamed or symlinked by the performer at any time. A daemon read or write must never follow
a performer-planted link to a host file. This spec closes the ten items in issue #220.

## Decisions

1. **State store inside a mount.** `StateStore` loads and saves by path. Config load now refuses a
   state directory that is inside or equal to any performer mount (`performer_log_dir`, an endpoint
   volume). The rule is the 219 overlap rule: resolved paths, case folded where the filesystem folds
   case, NFC before any fold, `~` expanded (`ProjectConfiguration._validate_daemon_paths_outside_mounts`).
   The attempt log's own refusal (#208) is unchanged.
2. **Mount identity by inode.** `mounted_file` resolves each root once per call and records its
   `(st_dev, st_ino)`. While walking, the walk also stats each directory it reaches: a directory whose
   identity equals a root's is a mount, even at a second path (bind mount, volume mounted twice,
   a macOS firmlink). The outermost hit wins, as before. Limit: only the listed roots are identified.
   A bind mount of a *subdirectory* of a mount, reached at a second path, is not identified.
3. **Cost of the walk.** Roots are resolved, case-probed and identified once per call instead of at
   every component. `/api/config/all`, `/api/config/routing` (the table read) and the persona
   classifier's CLAUDE.md and AGENTS.md reads run through `asyncio.to_thread`.
   Measured with `read_mounted_file` on a file nine levels deep, seven mounts, outside every mount,
   on this macOS machine (2000 reads each):

   | | per read |
   | --- | --- |
   | before | 4678 us (run 1), 4.7 ms |
   | after | 440 us (run 1), 453 us (run 2) |
   | plain `read_bytes` | 25 us, 21 us |

   The issue measured about 2 ms before; this machine is slower. The ratio is the point: about 10x.
4. **Temp-name swap in `_rename_into`.** The temporary file's name is checked against the descriptor
   it was written through (`fstat` on the fd against `lstat` on the name) before the rename, and
   again after it. A mismatch removes the planted entry and fails the replacement; the host file is
   never written. O_TMPFILE with `linkat` was not used: it cannot replace an existing leaf, and it is
   not available on macOS. Residual: POSIX renames act on names, so a swap inside the window between
   the check and the rename is detected afterwards, not prevented.
5. **token_usage transcript opens.** `TokenLedger.load_from_transcripts` takes `open_file`. The daemon
   passes the mounted-file reader (`open_mounted_file`, a new streaming variant of the same walk and
   refusals, with no size bound). The default refuses a link at the leaf. The opener is injected
   because `token_usage` and `run_transcript` import each other, so a direct import would cycle.
6. **`prune_run_logs`.** Not unused: the run-log retention loop in `daemon.py` calls it daily. The
   root is opened `O_NOFOLLOW`, and entries are listed, checked and removed by name relative to that
   descriptor. A link at the root deletes nothing.
7. **SIGUSR1 task dump.** Written by `write_task_dump` into a directory from `mkdtemp` (mode 0700),
   opened `O_NOFOLLOW | O_TRUNC`, mode 0600. The fixed `/tmp/coordinare-tasks.log` is gone.
8. **`workspace_root`.** Audit: no performer container receives a clone. Docker `-v` arguments come
   only from `config.volumes` (`performer_lifecycle.py`), Kubernetes mounts come from the same list,
   and the HTTP payload carries a URL, branch and token, not a path. The subprocess transport runs
   the performer on the host, not in a container, so it is outside the mount model. Reachability
   therefore depends on config: an endpoint volume that contains `workspace_root` (or the temp dir
   it defaults to) would expose the clones. Config load now refuses a `workspace_root` inside a
   performer mount, and a default temp dir inside one (`ProjectConfiguration._validate_daemon_paths_outside_mounts`).
9. **Clone roots in `performer_mount_hosts`.** No-op, pinned by a test. No clone directory is
   bind-mounted into a performer from the host, so there is nothing to add. Clones are not in the
   mount list and cannot be, because they are created per card.
10. **Dispatch mount list.** `dispatch_performer._performer_mounts` builds the list from
    `coordinare_config.global_config`, not from the symphony's effective config, so an override that
    drops a volume cannot shorten it. Without a loaded daemon config it falls back to `state["config"]`.

## Config that now fails to load (deploy risk)

- `state_file_path` (or its directory) inside or equal to `performer_log_dir` or an endpoint volume.
- `workspace_root` inside or equal to `performer_log_dir` or an endpoint volume.
- With no `workspace_root`, an endpoint volume or log dir that contains the system temp directory.

All twelve shipped `config.example*.yaml` files load under the new rules. The Helm chart sets
`state_file_path` to `/var/lib/coordinare/coordinare.state.json` and declares no endpoint volumes in
its values, so it is unaffected by these rules.

## Log vocabulary

No new events. `mounted_file.refused` (path, reason) is unchanged and now also covers the
temporary-name checks (`temporary file replaced before the rename`, `replacement swapped after the rename`).

## Tests

- `tests/unit/test_220_mount_identity.py`: a symlinked alias, a second path to a mount (macOS firmlink,
  `/proc/self/root` on Linux), a second path to a mount root, and the once-per-call root resolution.
- `tests/unit/test_220_temp_name_swap.py`: a swap before the rename and a swap after the check.
- `tests/unit/test_220_transcript_logs_and_tasks.py`: the transcript opener, the ledger rebuild, `prune_run_logs` at a link root, and the task dump.
- `tests/unit/test_220_config_mount_rules.py`: the state and workspace rules, case and NFC variants, the default temp dir.
- `tests/unit/test_220_off_loop_and_dispatch.py`: the three reads off the loop, the global mount list, and the clone-root no-op.
- Adjusted existing tests: `test_134_review_fixes.py` (the mid-read failure is now injected through the opener) and `test_main_coverage.py` (the SIGUSR1 source guard checks `write_task_dump`).

What cannot be tested locally: a real bind mount (needs root or a user namespace). The identity
branch is exercised on macOS through the firmlink, and on Linux only through the string path,
because `/proc/self/root` is unified by `resolve()`.

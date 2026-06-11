# Contract: `devenv-profile.sh` deterministic native-lib activation

This is a **shell behavioral contract** for the coordinare-owned, image-baked profile
script `agent/performer/devenv-profile.sh`. There is no HTTP/JSON dispatch surface and no
`## Field Registry` (no new payload fields are introduced by this feature). Listed here so
`/speckit.analyze` can map requirements to verifiable behaviors.

## Inputs (environment + filesystem)

| Input | Meaning |
|-------|---------|
| `/devenv/*/` mounts | read-only env caches |
| `/devenv/<slug>/debs/*.deb` | captured packages with shared objects |
| `/devenv/<slug>/activate.sh` | LLM-authored activation (sourced after deterministic step) |
| `_DEVENV_SOURCED` (env) | re-entry guard; if set, whole script is a no-op |
| `_DEVENV_LIB_BASE` (env, optional) | writable base dir override (default fixed image path) |

## Outputs (environment + filesystem)

| Output | Meaning |
|--------|---------|
| `<lib_base>/<slug>/lib/*.so*` | extracted shared objects (one-time) |
| `<lib_base>/<slug>/.extracted` | sentinel marking completed atomic publish |
| `LD_LIBRARY_PATH` | each cache's lib dir prepended; prior value preserved |

## Behavioral guarantees (asserted by tests)

1. **C1 (FR-001/FR-002)**: Given a cache with `debs/` containing a `.so`-bearing `.deb`,
   after sourcing, the `.so` is present under `<lib_base>/<slug>/lib` and that dir is on
   `LD_LIBRARY_PATH`.
2. **C2 (FR-004)**: The lib dir is on `LD_LIBRARY_PATH` even when `activate.sh` contains no
   deb/`LD_LIBRARY_PATH` logic (today's verbatim script).
3. **C3 (FR-003)**: With the sentinel already present, sourcing does NOT invoke `dpkg-deb`
   again but still ensures the lib dir is on `LD_LIBRARY_PATH`.
4. **C4 (FR-008)**: A cache with no `debs/` dir leaves `LD_LIBRARY_PATH` unchanged for that
   cache and the shell starts cleanly.
5. **C5 (FR-005/SC-005)**: A malformed/corrupt `.deb`, or an unwritable lib base, does NOT
   abort the shell (exit 0) and does NOT prevent other caches/debs from processing.
6. **C6 (FR-006)**: `_DEVENV_SOURCED` re-entry guard still short-circuits the entire script
   (including the new extraction logic) — no recursion, idempotent re-source.
7. **C7 (FR-002 scenario 2)**: If `activate.sh` also appends to `LD_LIBRARY_PATH`, the
   result is well-formed and contains both our lib dir and theirs.
8. **C8 (FR-007/SC-004)**: A command run via `sh -c` observes the same activated `PATH`
   and `LD_LIBRARY_PATH` as via `bash -c` (via inheritance from the activated parent or
   direct sourcing).
9. **C9 (FR-005)**: The script never enables `set -e`/`set -u`, never calls `exit`, and
   never `return`s non-zero to the sourcing shell.

## Non-goals

- Installing packages into the system dpkg database (`dpkg -i`/`apt`). Out of scope.
- Synthesizing missing debs the bootstrap failed to capture. Out of scope (FR-010 keeps
  capture as the source).
- Changing the coordinare dispatch payload schema. No new fields.

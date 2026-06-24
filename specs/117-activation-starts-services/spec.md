# Spec 117: Activation starts declared services in the performer container

## Problem / insight

Stateful services (postgres/redis) have **container-local** runtime state — `services-start.sh`
writes its data + pid files under `${XDG_RUNTIME_DIR:-/tmp}/coordinare-services`, never the
(read-only, shared) cache mount. The 091→101 design started the services inside the
**env-bootstrap container** (via the `run_service_readiness` gate). That container exits when
bootstrap finishes, so the running postgres/redis never existed in the **QA performer's**
container — the one whose app-under-test actually connects to them. Starting services at
bootstrap was aimed at the wrong process.

The correct trigger is **activation**: when any performer container mounts the cache and
activates the env (via `BASH_ENV=/etc/devenv-activate.sh` → `devenv-profile.sh`), that
activation should start postgres/redis from the binaries the bootstrap installed into the
cache — so the services are live in the same container that uses them.

This splits responsibilities cleanly and matches spec 116 (performer-owned services):
- **Bootstrap performer** installs the service binaries into the cache (debs → `services-extract/`).
- **Activation** (every performer container) starts them from those cached binaries, writing
  runtime state to the container-local writable dir.

## Requirements

1. `devenv-profile.sh` (the image's activation entrypoint, sourced via `BASH_ENV`) MUST, after
   extracting native libs and sourcing the cache's `activate.sh`, invoke
   `<cache>/services/services-start.sh` when present and readable — so declared services start
   on activation in the performer container.
2. It MUST run **once per container** (process tree), bounded by the existing `_DEVENV_SOURCED`
   re-entry guard the profile body already lives inside — NOT on every subshell source.
3. It MUST be **best-effort and non-fatal**: the profile is sourced into EVERY shell and MUST
   NEVER abort the caller (the file's standing invariant). A service that won't start is logged
   to a per-cache file (`<lib_base>/<slug>/services-start.log`) and a one-line stderr notice;
   the profile continues. `services-start.sh` is already idempotent (skips running PIDs / bound
   ports) and self-bounds its readiness wait, so re-runs and partial prior starts converge.
4. Runs AFTER `activate.sh` is sourced, so `$DEVENV`/`PATH`/`LD_LIBRARY_PATH` (incl. spec-115's
   service-extract lib path inside `services-start.sh`) are in place for the service binaries.
5. Read-only cache compatibility: `services-start.sh` is read from the (possibly RO) cache mount
   and writes runtime state only to the writable `/tmp` + the writable `<lib_base>` log path —
   no writes to the cache mount.
6. Make the cache root overridable via `_DEVENV_ROOT` (default `/devenv`), mirroring the existing
   `_DEVENV_SYSROOT` / `_DEVENV_LIB_BASE` test seams, so the profile's cache loop (and the new
   services-start step) is unit-testable without writing to `/devenv`.

## Out of scope

- WHO installs the binaries into the cache (spec 116: the performer; unchanged here).
- The coordinare-side `coordinare_manages_services` toggle (116) — orthogonal; this is
  performer-image activation behavior that runs whenever the cache declares services, regardless
  of that toggle.
- The 101 `run_service_readiness` bootstrap gate (left skipped by default per 116).

## Acceptance

- Sourcing `devenv-profile.sh` with `_DEVENV_ROOT` pointing at a cache that contains
  `services/services-start.sh` runs that script exactly once and does not abort the shell.
- A `services-start.sh` that exits non-zero is logged, not fatal (the sourcing shell survives).
- No `services-start.sh` (or no `services/` dir) → no-op.
- Live: a performer container (QA) mounting the website cache starts postgres + redis on
  activation; `pg_isready` reports accepting connections inside that container.

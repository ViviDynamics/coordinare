# Implementation Plan: Resolve Declared Service Hostnames to Loopback In-Container

**Branch**: `105-service-host-aliasing` | **Spec**: [spec.md](spec.md)

## Technical Context

**Language**: Python 3.14 (host-side render) emitting POSIX/bash sourced in the Debian performer.
**Primary deps**: the coordinare-owned `render_activate_sh` (`src/coordinare/services/env_manifest.py`) — reused/extended. No new dependency, no schema change.
**State**: none.
**Testing**: `.venv/bin/pytest` (tests in `tests/unit/test_env_manifest.py`); TDD.
**Scope**: a single appended block in the rendered `activate.sh`. Host-side render → ships via coordinare restart + re-bootstrap (regenerates activate.sh), like spec-103. No image rebuild.

## Constitution Check

- **TDD (II)**: failing tests first (content assertions + a behavioral run of the rendered script against a temp hosts file). PASS.
- **Secret invariant**: the rendered `activate.sh` carries a generic runtime loop that reads `*_HOST`/`*_HOSTNAME` values from the live env — NO test-env value is baked into the script. PASS.
- **Minimal surface / no new dep / image stays service-agnostic**: PASS.

## Design

`render_activate_sh` already emits runtime blocks (PATH globs, the spec-103 postgres-bin glob). Append a **service-host aliasing** block that runs at activation (sourced in-container, live test-env present):

```sh
# --- service host aliasing (spec 105): declared service hostnames -> loopback ---
# Coordinare hosts declared services on 127.0.0.1 in THIS container; the app's
# test-env names them by docker-compose hostnames (e.g. POSTGRESQL_HOST=db).
# Map single-label *_HOST/*_HOSTNAME values to loopback so the app connects.
# Best-effort; reads hostnames from the live env (no secret value is baked in).
_HOSTS="${COORDINARE_HOSTS_FILE:-/etc/hosts}"
for _hv in $(env | sed -n 's/^[A-Za-z0-9_]*_HOSTNAME=//p; s/^[A-Za-z0-9_]*_HOST=//p'); do
  case "$_hv" in ""|localhost|*.*|*:*) continue ;; esac   # skip empty/localhost/FQDN/IP/host:port
  grep -qw "$_hv" "$_HOSTS" 2>/dev/null && continue        # idempotent
  echo "127.0.0.1 $_hv" >> "$_HOSTS" 2>/dev/null || true   # best-effort, non-fatal
done
```

### Key choices

- **`*_HOST` / `*_HOSTNAME` values, not URL parsing**: the same hostname that appears in `redis://redis:.../` is also declared by `REDIS_HOST=redis`; aliasing the *name* fixes both. No URL parsing needed (FR-002).
- **Single-label only** (`*.*`/`*:*`/`localhost`/empty skipped): never clobber FQDNs/IPs/loopback (FR-003).
- **`grep -qw` idempotency** + `>> … || true` best-effort (FR-004/FR-005).
- **`COORDINARE_HOSTS_FILE` seam**: defaults to `/etc/hosts`; lets tests point at a temp file (testability; harmless in prod).
- **Reads live env at runtime** → the rendered script is the generic loop, secret-free (FR-006).

### Why activate.sh

It is the coordinare-owned env-setup script, sourced in-container with the live (injected) test-env present, before the app/tests run — the natural, secret-free home. Host-side render (env_manifest.py) means no image rebuild.

## Phase 0 — research.md
One decision: alias-by-`*_HOST`-name vs URL-parse vs env-override vs catch-all resolver vs sidecars. Chosen: alias single-label `*_HOST`/`*_HOSTNAME` values in `/etc/hosts`. Rationale + rejected alternatives in [research.md](research.md).

## Phase 1 — contracts + quickstart
- [contracts/service-host-aliasing.md](contracts/service-host-aliasing.md): the rendered-block contract + invariants.
- [quickstart.md](quickstart.md): replays the website db/redis case + skip cases + idempotency.
- data-model: no change.

## Phase 2 — tasks (see tasks.md)
MVP = US1 (single-label alias). US2 (URL via same name) is a consequence; US3 (skip/idempotent) are guards on the same block.

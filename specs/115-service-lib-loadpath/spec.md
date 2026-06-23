# Spec 115: Service-binary library load path (libpq.so.5)

Continues the env-cache / stateful-service line (091 service hosting; 102/106 server-deb
fetch; 103 server-bin-on-PATH; 110/111 postgres init/timing; 114 services-start runner
timeout). Spec 114 removed the false 120s-timeout kill and exposed the true blocker.

## Problem (root cause of the website Postgres bootstrap stall, confirmed live + reproduced 2026-06-23)

The coordinare-managed service binaries (postgres `initdb`/`postgres`, redis `redis-server`)
are placed on `PATH` by `activate.sh` (spec 103 glob over `<cache>/*/usr/lib/postgresql/*/bin`
and `<cache>/*/usr/bin`), but **nothing puts their shared libraries on `LD_LIBRARY_PATH`**.

The performer profile (`devenv-profile.sh`) flattens `*.so` ONLY from `<cache>/debs/*.deb`
into a dedicated lib dir (`/var/lib/devenv/<slug>/lib`) that it prepends to `LD_LIBRARY_PATH`.
The LLM-driven deb-fetch persona (102/106) landed postgresql-17's dependency closure
(libicu76, libllvm19, libjemalloc2, libzstd, …) but NOT `libpq5` (provider of `libpq.so.5`)
nor the `postgresql-17` server deb itself — only the `postgresql`/`postgresql-client`
metapackages. So `libpq.so.5` is absent from the profile's flattened lib dir.

Net at bootstrap: `services-start.sh` runs `initdb` →
`initdb: error while loading shared libraries: libpq.so.5: cannot open shared object file` →
`PG_VERSION` is never created → `postgres` can't start → the 180s `pg_isready` wait
(spec 111) times out → `services-start failed` (returncode 75) → `service_readiness_failed`
→ bootstrap fails → `cache_dir_ready=False` → the dispatch gate holds all website cards.

Crucially, the cache's `services-extract/` tree (the `dpkg-deb -x` of the fetched debs,
spec 102) DOES contain `libpq.so.5` (at `usr/lib/<arch>/libpq.so.5`) and the postgres
extension libs (at `usr/lib/postgresql/<NN>/lib`). They are present in the cache; they are
just not on the library search path. Verified live: adding those dirs to `LD_LIBRARY_PATH`
makes `initdb` load, postgres + redis start, `pg_isready` report "accepting connections".

## Requirements

1. The rendered `services-start.sh` MUST make the coordinare-managed service binaries
   (postgres, redis) able to load their shared libraries from the cache's `services-extract`
   tree — specifically the multiarch lib dir (`services-extract/usr/lib/*-linux-gnu`, where
   `libpq.so.5` lives) and the postgres lib dir (`services-extract/usr/lib/postgresql/*/lib`).
2. The library exposure MUST cover BOTH the service daemons (postgres/redis) AND the
   root-caller clients the script invokes (the `pg_isready` readiness probe, `psql`,
   `createdb`) — all of them link against `libpq.so.5`. It is exported WITHIN
   `services-start.sh` (a one-shot bootstrap process), NOT added to `activate.sh`. This is the
   correct boundary: the application-under-test (Rails/ruby/node at QA time) gets a fresh
   shell that sources `activate.sh` and does NOT inherit the bootstrap script's environment,
   so the broad `services-extract` closure never shadows system libs at QA time; only the
   backgrounded daemons the script launches inherit it — exactly what must link at runtime.
   `_pg_as`/the md5-auth init pass the path explicitly because `runuser` drops the env.
3. The scoped path MUST be additive: it prepends the discovered service lib dirs onto the
   existing (profile-set) `LD_LIBRARY_PATH`, so the profile's flattened-debs lib dir is still
   honored.
4. Discovery MUST be guarded: if `$DEVENV` is unset or a candidate dir is absent, the lookup
   is a no-op (no `set -u` abort, no bogus path entries).
5. Invariants (carried from 091/102/110/111/114): the script stays POSIX/bash sourced inside
   the Debian-family performer; runs under `set -u`/`set -o pipefail` without `set -e`; the
   service-inference templater stays the shared source of truth; no secret values touched;
   no new external dependency; no coordinare state change.

## Out of scope (YAGNI)

- Making the deb fetch deterministic / coordinare-owned so `libpq5` + the server deb are
  always fetched (the durable follow-up; tracked separately). This spec relies on the
  `services-extract` tree already carrying the libs, which is true for the current cache and
  for any cache whose fetch extracted the server's deb tree.
- Global `LD_LIBRARY_PATH` changes in `render_activate_sh` / the profile (rejected: shadowing
  risk for the app under test).
- Any change to the 180s readiness wait, the runuser/pgrunner model, or the 114 outer timeout.

## Acceptance

- A rendered postgres `services-start.sh` exposes the `services-extract` lib dirs to the
  `initdb`/`postgres` invocations (both the trust-auth and md5-auth init paths) and to the
  generic/redis start, scoped (not via global export).
- Templater unit tests assert the scoped `LD_LIBRARY_PATH` prelude is emitted and used by the
  managed-service invocations, and that it is NOT a global `export`.
- Live: website env-bootstrap reaches `bootstrap_complete success=True`; postgres + redis are
  accepting connections; the dispatch gate opens and a website card dispatches.

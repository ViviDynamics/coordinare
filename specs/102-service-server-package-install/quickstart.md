# Quickstart: Env-Bootstrap Delivers Runnable Service Server Binaries

Replays the website Postgres install gap (the meta-only fetch) as acceptance scenarios.

## Scenario A — US1: the versioned server is fetched, not just the meta (SC-001)
1. A symphony declares a postgres service; the bootstrap renders the service-install block.
2. **Verify:** the rendered fetch uses `apt-get install --download-only -o Dir::Cache::archives=<cache>/debs/ postgresql postgresql-client` (closure-resolving) — NOT the old `apt-get download $(apt-cache depends … | grep)`. When run, `<cache>/debs/` contains the versioned `postgresql-NN` server, and extraction yields runnable `initdb`/`pg_ctl`/`postgres`.

## Scenario B — US1: passes the spec-101 readiness gate (SC-002)
1. With the server binaries present, the bootstrap reaches the 101 service-readiness gate.
2. **Verify:** postgres starts + is connectable → bootstrap completes (the website-class "declared service, zero server binaries" failure cannot recur).

## Scenario C — US2: version-resilient (SC-003)
1. The rendered instruction names the `postgresql` meta (no `postgresql-17` literal).
2. **Verify:** on distro N the closure resolves `postgresql-N's-server`; on N+1 it resolves N+1's — no code edit pinned to one version breaks it.

## Scenario D — US3: general across kinds (SC-004)
1. Render the block for `redis` (and any future kind).
2. **Verify:** the same closure-resolving command renders for every coordinare-known kind; redis-server (concrete) downloads with deps; a meta-named kind resolves its server. No per-kind special-casing.

## Scenario E — no services unchanged
1. A symphony declares no stateful services.
2. **Verify:** the service-install block is empty (persona byte-for-byte unchanged).

## Scenario F — fail-clear (SC-005)
1. The server package is unobtainable for the arch/distro.
2. **Verify:** the fetch fails (and spec-101 then blocks the bootstrap with a clear cause) — never a silent meta/client-only install marked complete. Instruction + logs carry no secrets.

## Real-world payoff
Re-bootstrapping the website cache now fetches `postgresql-NN` (the real server), so `initdb`/`pg_ctl`/`postgres` are present → the services manifest resolves → postgres starts → spec-101 passes → feature/QA tests get a live DB → #177 can actually go green.

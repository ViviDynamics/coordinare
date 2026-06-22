# Quickstart: Env-Bootstrap Service-Readiness Completion Gate

Replays the website Postgres failure (cards #168→#177) as acceptance scenarios.

## Scenario A — US1: rejected manifest + required service → bootstrap fails (SC-001/SC-002)
1. A symphony declares a required service (postgres); service inference is **rejected** (no usable manifest — the live website case: server binary unresolvable).
2. **Verify:** the bootstrap returns **error** (not `env_bootstrap_complete`), reason names the service + "manifest rejected: <reason>"; the coordinare leaves the cache **not ready** (readme_sha=None, cache_dir_ready false) and does NOT dispatch cards into it.

## Scenario B — US1: required service won't connect → bootstrap fails (SC-001)
1. Required service declared, manifest present, but `services-start.sh`/`services-health.sh` can't make it connectable (e.g. no server binary → start fails, or health never passes).
2. **Verify:** bootstrap **error**, reason "service not connectable: postgres"; cache not ready.

## Scenario C — US2: required service connectable → completes (SC-003)
1. Required service declared, manifest present, services start and `services-health.sh` exits 0 (connect succeeds).
2. **Verify:** `env_bootstrap_complete`; cache marked ready/current.

## Scenario D — US2: no declared services → unchanged (SC-003)
1. A symphony declares no services.
2. **Verify:** bootstrap completes exactly as before — no readiness gate, no new failure surface.

## Scenario E — US1: optional service fails → warn, not block (FR-004)
1. A service explicitly marked optional fails its health check.
2. **Verify:** bootstrap still completes (warn logged); only **required** services block.

## Scenario F — US3: observability + bounded (SC-004/SC-005)
1. Trigger a required-service readiness failure.
2. **Verify:** an `env_bootstrap.service_readiness` record names the service + status + reason with NO secret values (no DB password); the check is bounded (a down service surfaces within the timeout+retry, never hangs the bootstrap).

## Real-world payoff
Had this gate existed, the website cache would have **failed its bootstrap** (postgres manifest rejected / server missing / unconnectable) and re-bootstrapped + surfaced "postgres not connectable" — instead of marking ready and sending #168 into hours of thrashing against a dead DB.

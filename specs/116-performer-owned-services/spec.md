# Spec 116: Restore performer-owned env setup (disable coordinare-managed services by default)

## Problem / decision

The stateful-service-hosting line (091→115) moved service hosting OUT of the env-bootstrap
performer and INTO coordinare: coordinare injects a rigid deb-fetch persona, renders
`services-{start,health,stop}.sh`, declares "coordinare-managed kinds" (postgres/redis via
`.coordinare/score.json`), and gates the bootstrap on its own `run_service_readiness` check.
This subsystem never reliably worked end-to-end (it produced a 15-spec cascade of bugs and
still does not host postgres for the website QA env — the LLM bootstrap agent does not
faithfully run the dictated apt fetch, so the server deb + `libpq5` never land in the cache).

Decision: **revert to the pre-091 behavior where the env-bootstrap PERFORMER owns environment
setup end-to-end** — it runs service inference, writes its own scripts, and is verified by the
toolchain `verify.sh`; coordinare stops injecting the service persona, rendering service
scripts, and gating service readiness. Implemented as a default-OFF config toggle so the
091→115 code stays in the tree (dormant, re-enablable) rather than being deleted.

## Requirements

1. Add `EnvCacheConfig.coordinare_manages_services: bool` defaulting to **False** (mounted at
   `global_config.env_cache`). When False, coordinare does NOT manage stateful services.
2. When `coordinare_manages_services` is False, the env-cache bootstrap MUST:
   - NOT fetch/declare coordinare-managed services (`declared_services` is empty for the
     dispatch), which already cascades to OMITTING the "SYSTEM SERVICES (stateful)" deb-fetch
     persona block (it is additive on a non-empty `declared_services`).
   - NOT call `write_service_scripts` (the performer's own `apply_manual_override` writes the
     service scripts, as it did pre-108).
3. The flag MUST reach the performer so it can skip the spec-101 readiness gate. Carry it on
   `BootstrapJobPayload.coordinare_manages_services` → (via `payload.model_dump()` →
   `card_context` → `JobInitPayload.metadata`) → the performer's `Score` model (declared
   explicitly, since `Score` is `extra="ignore"` and would otherwise drop it).
4. In the performer env_bootstrap completion flow, when `coordinare_manages_services` is False,
   SKIP `run_service_readiness` entirely (pre-101 behavior: the bootstrap's success criterion
   is the toolchain `verify.sh`, not coordinare-orchestrated service start/health). The
   performer's own service inference (`_run_service_inference` → `apply_manual_override`) still
   runs and writes `services.json` + scripts, exactly as before the demotion.
5. When `coordinare_manages_services` is True, behavior is UNCHANGED from spec 115 (the whole
   091→115 path is preserved and re-enablable via config).
6. Register the new `coordinare_manages_services` dispatch field in
   `specs/contracts/dispatch-payload.md`.

## Invariants (preserved, NOT part of the demotion)

- 088 bootstrap budget / `bootstrap_exhausted`, 092 test-env injection, 093 toolchain-readiness
  dispatch gate (`verify_env_cache_clean`), 094 board reconcile — all UNCHANGED and still
  active. The toggle only governs the coordinare-managed-SERVICES surfaces.
- No secret values in logs/records. No new external dependency. Single-host snapshot state
  model unchanged (the flag is config, not persisted state).

## Out of scope

- Deleting the 091→115 code (kept dormant behind the toggle).
- Any new approach to actually hosting postgres/redis — that returns to being the performer's
  responsibility (the pre-091 reality), or a future, separate effort.

## Acceptance

- With the default config (`coordinare_manages_services=False`): a website env-bootstrap dispatch
  carries no service-install persona block, coordinare writes no service scripts, the performer
  skips `run_service_readiness`, and bootstrap success is decided by the toolchain `verify.sh`
  alone — the dispatch gate then opens and website cards dispatch (no longer blocked on the
  unhostable postgres).
- With `coordinare_manages_services=True`: the 091→115 behavior is bit-for-bit preserved
  (existing tests for the managed path still pass).
- Coordinare + performer unit suites pass; the new field is registered in the dispatch contract.
